from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from recordian.audio_feedback import play_sound
from recordian.backend_manager import BackendManager
from recordian.config import ConfigManager
from recordian.runtime_config import normalize_commit_backend, normalize_notify_backend, normalize_runtime_config
from recordian.setting_effects import SettingEffect, combined_setting_effect, effect_label, effect_status_message
from recordian.tray_settings_utils import KEY_LABEL_MAP
from recordian.tray_utils import (
    blend_hex,
    build_gtk_hotkey_spec,
    export_auto_lexicon_db,
    hex_with_alpha,
    import_auto_lexicon_db,
    next_event_poll_delay_ms,
    normalize_hotkey_token,
    overlay_hide_delay_seconds,
    parse_bool,
    save_config_changes,
    truncate,
)
from recordian.waveform_renderer import WaveformRenderer

from recordian.tray_menu import (
    build_appindicator_menu,
    collect_recent_runtime_rows,
    get_logo_path,
    list_tray_refine_presets,
    refresh_appindicator_preset_submenu,
    status_summary_label,
    sync_appindicator_preset_submenu,
    update_tray_menu,
)
from recordian.tray_diagnostics import (
    collect_runtime_diagnostics,
    format_diagnostic_report,
)
from recordian.tray_context_editor import open_context_editor
from recordian.tray_speaker_wizard import open_speaker_enrollment_wizard
from recordian.tray_settings import load_hotkey_default_config, open_settings_gtk

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"
DEFAULT_AUTO_LEXICON_DB_PATH = "~/.config/recordian/auto_lexicon.db"

HOTKEY_CAPTURE_FIELDS = {"hotkey", "stop_hotkey", "toggle_hotkey"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recordian tray GUI with waveform overlay.")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--notify-backend", choices=["none", "auto", "notify-send", "stdout"], default="auto")
    return parser


@dataclass(slots=True)
class RecentRunObservation:
    text: str = ""
    detected_language: str = ""
    asr_provider: str = ""
    asr_path: str = ""
    asr_capabilities: str = ""
    record_ms: float = 0.0
    transcribe_ms: float = 0.0
    refine_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.record_ms + self.transcribe_ms + self.refine_ms


@dataclass(slots=True)
class UiState:
    status: str = "idle"
    detail: str = "Idle"
    backend_running: bool = False
    last_run: RecentRunObservation = field(default_factory=RecentRunObservation)
    last_run_timestamp: float = 0.0


class TrayApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config_path = Path(args.config_path).expanduser()
        self.state = UiState()
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self._warmup_done = False
        self._off_sound_after_id: str | None = None
        self._off_cue_armed = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Recordian Tray")

        self.overlay = WaveformRenderer(self.root)
        self.indicator = None

        self.backend = BackendManager(
            self.config_path,
            self.events,
            on_state_change=self._on_backend_state_change,
            on_menu_update=self._update_tray_menu,
        )
        self._gtk_settings_window: Any = None
        self._diagnostics_window: tk.Toplevel | None = None
        self._diagnostics_text: tk.Text | None = None
        self._diagnostics_status_var: tk.StringVar | None = None
        self._diagnostics_refresh_button: tk.Button | None = None
        self._diagnostics_report = ""
        self._appindicator_preset_submenu: Any = None
        self._appindicator_preset_items: dict[str, Any] = {}
        self._appindicator_preset_names: list[str] = []
        self._preset_menu_last_sync_ts = 0.0
        self._config_cache: dict[str, Any] | None = None
        self._config_cache_mtime: float = 0.0
        self._toggle_lock = threading.Lock()

    def _get_cached_config(self) -> dict[str, Any]:
        try:
            mtime = self.config_path.stat().st_mtime
        except Exception:
            mtime = 0.0
        if self._config_cache is not None and mtime == self._config_cache_mtime:
            return self._config_cache
        self._config_cache = ConfigManager.load(self.config_path)
        self._config_cache_mtime = mtime
        return self._config_cache

    def _invalidate_config_cache(self) -> None:
        self._config_cache = None
        self._config_cache_mtime = 0.0

    def _on_backend_state_change(self, running: bool, status: str, detail: str) -> None:
        """线程安全的状态更新回调"""
        def _update():
            self.state.backend_running = running
            self.state.status = status
            self.state.detail = detail
        self.root.after(0, _update)

    def run(self) -> None:
        self._start_tray()
        if not self.args.no_auto_start:
            self.backend.start()
        self.root.after(next_event_poll_delay_ms(handled_events=0), self._poll_events)
        self.root.mainloop()

    def _poll_events(self) -> None:
        handled_events = 0
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            handled_events += 1
            self._handle_event(event)
        self.root.after(next_event_poll_delay_ms(handled_events=handled_events), self._poll_events)

    def _handle_event(self, event: dict[str, object]) -> None:
        et = str(event.get("event", ""))
        if et == "ready":
            self.state.backend_running = True
            self.state.status = "idle"
            self.state.detail = "Ready"
            if self._warmup_done:
                self.overlay.set_state("idle", "Ready")
        elif et == "model_warmup":
            status = str(event.get("status", ""))
            if status == "starting":
                self.state.backend_running = True
                self.state.status = "warming"
                self.state.detail = "Model warmup..."
            elif status == "ready":
                self._warmup_done = True
                latency_ms = cast(float, event.get("latency_ms", 0.0) or 0.0)
                self.state.status = "idle"
                self.state.detail = f"Warmup ready ({latency_ms:.0f}ms)"
        elif et == "recording_started":
            self._off_cue_armed = True
            self._cancel_off_cue_timer()
            self.state.status = "recording"
            self.state.detail = "Recording..."
            self.overlay.set_state("recording", "Listening...")
            self.root.after(0, lambda: self._play_global_cue("on"))
        elif et == "voice_wake_triggered":
            keyword = str(event.get("keyword", "")).strip()
            self.state.detail = f"已唤醒: {keyword}" if keyword else "已语音唤醒"
        elif et == "stream_partial":
            text = str(event.get("text", "")).strip()
            if text:
                self.state.last_run.text = text
                if self.state.status == "recording":
                    self.state.detail = "Recording..."
                elif self.state.status == "processing":
                    detail = truncate(text, 48)
                    self.state.detail = detail
                    self.overlay.set_state("processing", detail)
        elif et == "realtime_asr_partial":
            text = str(event.get("text", "")).strip()
            if text:
                self.state.last_run.text = text
                detail = truncate(text, 48)
                self.state.detail = detail
                if self.state.status == "recording":
                    self.overlay.set_state("recording", detail)
                elif self.state.status == "processing":
                    self.overlay.set_state("processing", detail)
        elif et == "refine_stream_chunk":
            text = str(event.get("accumulated", "")).strip()
            if text:
                self.state.last_run.text = text
                if self.state.status == "processing":
                    detail = truncate(text, 48)
                    self.state.detail = detail
                    self.overlay.set_state("processing", detail)
        elif et == "audio_level":
            self.overlay.set_level(cast(float, event.get("level", 0.0) or 0.0))
        elif et == "processing_started":
            self.state.status = "processing"
            self.state.detail = "Processing..."
            detail = "Recognizing..."
            self.overlay.set_state("processing", detail)
            self._schedule_off_cue_from_overlay("processing", detail)
        elif et == "result":
            result = event.get("result")
            observation, commit_info = self._extract_recent_run_observation(result)
            self.state.last_run = observation
            self.state.last_run_timestamp = time.time()
            self.state.status = "idle"
            commit_backend = str(commit_info.get("backend", ""))
            commit_detail = str(commit_info.get("detail", ""))
            committed = bool(commit_info.get("committed", False))
            log_suffix = self._format_recent_run_log_suffix(observation)
            if observation.text:
                print(
                    (
                        f"result text={observation.text} committed={committed} backend={commit_backend} "
                        f"detail={commit_detail}{log_suffix}"
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    (
                        f"result text=<empty> committed={committed} backend={commit_backend} "
                        f"detail={commit_detail}{log_suffix}"
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            if observation.text:
                if committed:
                    self.state.detail = truncate(observation.text, 42)
                else:
                    detail = str(commit_info.get("detail", "not_committed"))
                    self.state.detail = truncate(f"已识别(未上屏): {observation.text}", 42)
                    self.events.put({"event": "log", "message": f"commit_failed: {detail}"})
                detail = truncate(observation.text, 48)
                self.overlay.set_state("idle", detail)
                self._schedule_off_cue_from_overlay("idle", detail)
            else:
                self.state.detail = "识别为空"
                detail = "No speech detected"
                self.overlay.set_state("idle", detail)
                self._schedule_off_cue_from_overlay("idle", detail)
        elif et == "busy":
            self.state.status = "busy"
            self.state.detail = "Busy"
            self.overlay.set_state("processing", "Still processing previous input")
        elif et == "error":
            self.state.status = "error"
            self.state.detail = str(event.get("error", "error"))
            detail = truncate(self.state.detail, 72)
            self.overlay.set_state("error", detail)
            self._schedule_off_cue_from_overlay("error", detail)
        elif et in {"stopped", "backend_exited"}:
            self.state.backend_running = False
            self.state.status = "stopped"
            self.state.detail = "Stopped"
            detail = "Stopped"
            self.overlay.set_state("idle", detail)
            self._schedule_off_cue_from_overlay("idle", detail)
        elif et == "log":
            msg = str(event.get("message", "")).strip()
            if msg:
                self.state.detail = truncate(msg, 48)
                if msg.startswith("diag "):
                    print(msg, file=sys.stderr, flush=True)
        self._update_tray_menu()

    @staticmethod
    def _extract_recent_run_observation(result: object) -> tuple[RecentRunObservation, dict[str, object]]:
        observation = RecentRunObservation()
        commit_info: dict[str, object] = {}
        if not isinstance(result, dict):
            return observation, commit_info

        observation.text = str(result.get("text", "")).strip()
        observation.detected_language = str(result.get("detected_language", "")).strip()
        observation.asr_provider = str(result.get("asr_provider", "")).strip()
        observation.asr_path = str(result.get("asr_path", "")).strip()
        observation.asr_capabilities = str(result.get("asr_capabilities", "")).strip()
        raw_commit = result.get("commit")
        if isinstance(raw_commit, dict):
            commit_info = raw_commit
        observation.record_ms = float(result.get("record_latency_ms", 0.0) or 0.0)
        observation.transcribe_ms = float(result.get("transcribe_latency_ms", 0.0) or 0.0)
        observation.refine_ms = float(result.get("refine_latency_ms", 0.0) or 0.0)
        return observation, commit_info

    @staticmethod
    def _format_recent_run_log_suffix(observation: RecentRunObservation) -> str:
        suffix = ""
        if observation.detected_language:
            suffix += f" lang={observation.detected_language}"
        if observation.asr_provider:
            suffix += f" provider={observation.asr_provider}"
        if observation.asr_path:
            suffix += f" path={observation.asr_path}"
        return suffix

    def _cancel_off_cue_timer(self) -> None:
        if self._off_sound_after_id is None:
            return
        try:
            self.root.after_cancel(self._off_sound_after_id)
        except Exception:
            pass
        self._off_sound_after_id = None

    def _schedule_off_cue(self, delay_s: float) -> None:
        self._cancel_off_cue_timer()
        delay_ms = max(0, int(max(0.0, delay_s) * 1000))

        def _play_off() -> None:
            self._off_sound_after_id = None
            self._off_cue_armed = False
            self._play_global_cue("off")

        self._off_sound_after_id = self.root.after(delay_ms, _play_off)

    def _schedule_off_cue_from_overlay(self, state: str, detail: str) -> None:
        if not self._off_cue_armed:
            return
        delay_s = overlay_hide_delay_seconds(self.overlay, state, detail)
        self._schedule_off_cue(delay_s)

    def _play_global_cue(self, cue: str) -> None:
        try:
            config = ConfigManager.load(self.config_path)
            custom_path = str(config.get("sound_on_path" if cue == "on" else "sound_off_path", "")).strip()
            legacy = str(config.get("wake_beep_path", "")).strip()
            play_sound(cue=cue, custom_path=custom_path, legacy_beep_path=legacy)
        except Exception:
            pass

    def toggle_text_refine(self, enabled: bool) -> None:
        """切换文本精炼；关闭时等同于快速模式。"""
        with self._toggle_lock:
            current = self._get_cached_config()
            if current.get("enable_text_refine") == enabled:
                return
            mode_text = "已启用文本精炼" if enabled else "已切换到快速模式"
            effect, restarted, _ = save_config_changes(
                self.config_path,
                {"enable_text_refine": enabled},
                apply_now=True,
                restart_callback=lambda: self.root.after(0, self.backend.restart),
            )
            self._invalidate_config_cache()
            self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})

            try:
                from .linux_notify import notify
                notify(effect_status_message(effect, restarted=restarted), title=f"Recordian: {mode_text}")
            except Exception:
                pass

            self._update_tray_menu()

    def toggle_quick_mode(self, enabled: bool) -> None:
        """兼容旧调用；快速模式开启时会关闭文本精炼。"""
        self.toggle_text_refine(not enabled)

    def toggle_voice_wake(self, enabled: bool) -> None:
        """切换语音唤醒模式"""
        with self._toggle_lock:
            current = self._get_cached_config()
            if current.get("enable_voice_wake") == enabled:
                return
            mode_text = "已开启语音唤醒" if enabled else "已关闭语音唤醒"
            effect, restarted, _ = save_config_changes(
                self.config_path,
                {"enable_voice_wake": enabled},
                apply_now=True,
                restart_callback=lambda: self.root.after(0, self.backend.restart),
            )
            self._invalidate_config_cache()
            self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})

            try:
                from .linux_notify import notify
                notify(effect_status_message(effect, restarted=restarted), title=f"Recordian: {mode_text}")
            except Exception:
                pass

            self._update_tray_menu()

    def toggle_auto_hard_enter(self, enabled: bool) -> None:
        """切换自动硬回车"""
        with self._toggle_lock:
            current = self._get_cached_config()
            if current.get("auto_hard_enter") == enabled:
                return
            mode_text = "已开启自动硬回车" if enabled else "已关闭自动硬回车"
            effect, restarted, _ = save_config_changes(
                self.config_path,
                {"auto_hard_enter": bool(enabled)},
                apply_now=True,
                restart_callback=lambda: self.root.after(0, self.backend.restart),
            )
            self._invalidate_config_cache()
            self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})

            try:
                from .linux_notify import notify
                notify(effect_status_message(effect, restarted=restarted), title=f"Recordian: {mode_text}")
            except Exception:
                pass

            self._update_tray_menu()

    def toggle_streaming_commit(self, enabled: bool) -> None:
        mode_text = "已开启流式上屏" if enabled else "已关闭流式上屏"
        with self._toggle_lock:
            current = self._get_cached_config()
            if current.get("enable_streaming_commit") == enabled:
                return
            effect, restarted, _ = save_config_changes(
                self.config_path,
                {"enable_streaming_commit": bool(enabled)},
                apply_now=True,
                restart_callback=lambda: self.root.after(0, self.backend.restart),
            )
            self._invalidate_config_cache()
            self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})

            try:
                from .linux_notify import notify
                notify(effect_status_message(effect, restarted=restarted), title=f"Recordian: {mode_text}")
            except Exception:
                pass

            self._update_tray_menu()

    def copy_last_text(self) -> None:
        """复制最后识别的文本到剪贴板"""
        if not self.state.last_run.text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.state.last_run.text)
            self.root.update()
            self.events.put({"event": "log", "message": f"已复制: {self.state.last_run.text[:30]}..."})
        except Exception as e:
            self.events.put({"event": "log", "message": f"复制失败: {e}"})

    def switch_preset(self, preset_name: str) -> None:
        """切换文字优化 preset"""
        effect, restarted, _ = save_config_changes(
            self.config_path,
            {"refine_preset": preset_name},
            apply_now=True,
            restart_callback=lambda: self.root.after(0, self.backend.restart),
        )
        self.events.put({"event": "log", "message": f"已切换到 {preset_name} preset（{effect_label(effect)}）"})
        self._update_tray_menu()

    # -- Delegated methods to new modules --

    def open_settings(self) -> None:
        """Open settings window (delegated to tray_settings)."""
        from recordian.tray_settings_utils import validate_settings_dict

        current = load_hotkey_default_config(include_sound_defaults=True)
        current.update(
            normalize_runtime_config(
                ConfigManager.load(self.config_path),
                include_sound_defaults=True,
                allow_auto_fallback_commit=False,
            )
        )
        current = normalize_runtime_config(
            current,
            include_sound_defaults=True,
            allow_auto_fallback_commit=False,
        )
        current = validate_settings_dict(current, defaults=load_hotkey_default_config(include_sound_defaults=True))
        current_record_backend = str(current.get("record_backend", "auto"))
        current_record_format = str(current.get("record_format", "ogg"))
        current_refine_provider = str(current.get("refine_provider", "local"))
        current_commit_backend = normalize_commit_backend(
            current.get("commit_backend", "auto"),
            allow_auto_fallback=False,
        )
        current_enable_thinking = current.get("enable_thinking", current.get("refine_enable_thinking", False))
        current_notify_backend = normalize_notify_backend(current.get("notify_backend", "auto"))

        if not (hasattr(self, "_glib") and hasattr(self, "_gtk")):
            self.events.put({"event": "log", "message": "GTK 未初始化，无法打开原生设置窗口"})
            return

        open_settings_gtk(
            self,
            current=current,
            current_record_backend=current_record_backend,
            current_record_format=current_record_format,
            current_refine_provider=current_refine_provider,
            current_commit_backend=current_commit_backend,
            current_enable_thinking=current_enable_thinking,
            current_notify_backend=current_notify_backend,
        )

    def open_context_editor(self) -> None:
        """打开常用词编辑器 (delegated to tray_context_editor)."""
        open_context_editor(self)

    def open_speaker_enrollment_wizard(self) -> None:
        """Open speaker enrollment wizard (delegated to tray_speaker_wizard)."""
        open_speaker_enrollment_wizard(self)

    def open_diagnostics(self) -> None:
        """Open diagnostics window."""
        window = self._diagnostics_window
        if window is not None and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.focus_force()
            self.refresh_diagnostics()
            return

        window = tk.Toplevel(self.root)
        window.title("Recordian 诊断状态")
        window.geometry("760x520")
        window.minsize(640, 420)

        header = tk.Label(
            window,
            text="用于快速确认后端、ASR 接口、模型名、唤醒与声纹配置是否一致。",
            anchor="w",
            justify="left",
        )
        header.pack(fill="x", padx=12, pady=(12, 6))

        status_var = tk.StringVar(value="准备检查…")
        status_label = tk.Label(window, textvariable=status_var, anchor="w", justify="left")
        status_label.pack(fill="x", padx=12, pady=(0, 8))

        text = tk.Text(window, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        button_row = tk.Frame(window)
        button_row.pack(fill="x", padx=12, pady=(0, 12))

        def _on_destroy() -> None:
            self._diagnostics_window = None
            self._diagnostics_text = None
            self._diagnostics_status_var = None
            self._diagnostics_refresh_button = None
            self._diagnostics_report = ""
            window.destroy()

        refresh_button = tk.Button(button_row, text="刷新", command=self.refresh_diagnostics)
        refresh_button.pack(side="left")

        copy_button = tk.Button(button_row, text="复制报告", command=self.copy_diagnostics_report)
        copy_button.pack(side="left", padx=(8, 0))

        close_button = tk.Button(button_row, text="关闭", command=_on_destroy)
        close_button.pack(side="right")

        window.protocol("WM_DELETE_WINDOW", _on_destroy)

        self._diagnostics_window = window
        self._diagnostics_text = text
        self._diagnostics_status_var = status_var
        self._diagnostics_refresh_button = refresh_button
        self.refresh_diagnostics()

    def refresh_diagnostics(self) -> None:
        text = self._diagnostics_text
        status_var = self._diagnostics_status_var
        refresh_button = self._diagnostics_refresh_button
        if text is None or status_var is None or refresh_button is None:
            return

        status_var.set("检查中…")
        refresh_button.configure(state="disabled")
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", "正在检查，请稍候…")
        text.configure(state="disabled")

        config_path = self.config_path
        config = ConfigManager.load(config_path)
        backend_proc = self.backend.proc
        backend_running = backend_proc is not None and backend_proc.poll() is None
        if backend_running:
            assert backend_proc is not None
        backend_pid = backend_proc.pid if backend_running else None  # type: ignore[union-attr]

        def _worker() -> None:
            try:
                rows = collect_runtime_diagnostics(
                    config,
                    config_path=config_path,
                    backend_running=backend_running,
                    backend_pid=backend_pid,
                )
                rows.extend(collect_recent_runtime_rows(self.state))
                report = format_diagnostic_report(rows)
                status_text = f"最近更新: {time.strftime('%H:%M:%S')}"
            except Exception as exc:
                report = f"[ERROR] 诊断失败: {type(exc).__name__}: {exc}"
                status_text = "诊断失败"

            def _apply() -> None:
                text_widget = self._diagnostics_text
                status_widget = self._diagnostics_status_var
                refresh_widget = self._diagnostics_refresh_button
                if text_widget is None or status_widget is None or refresh_widget is None:
                    return
                text_widget.configure(state="normal")
                text_widget.delete("1.0", "end")
                text_widget.insert("1.0", report)
                text_widget.configure(state="disabled")
                status_widget.set(status_text)
                refresh_widget.configure(state="normal")
                self._diagnostics_report = report

            self.root.after(0, _apply)

        threading.Thread(target=_worker, daemon=True, name="tray-diagnostics").start()

    def copy_diagnostics_report(self) -> None:
        if not self._diagnostics_report:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._diagnostics_report)
            self.root.update()
            self.events.put({"event": "log", "message": "诊断报告已复制到剪贴板"})
        except Exception as exc:
            self.events.put({"event": "log", "message": f"复制诊断报告失败: {exc}"})

    def _start_tray(self) -> None:
        try:
            import gi
            gi.require_version('AppIndicator3', '0.1')
            gi.require_version('Gtk', '3.0')
            from gi.repository import AppIndicator3, Gtk
            self._start_appindicator(AppIndicator3, Gtk)
        except (ImportError, ValueError) as e:
            raise RuntimeError(
                f"AppIndicator3 not available: {e}\n"
                "Please install: sudo apt install gir1.2-appindicator3-0.1"
            ) from e

    def _start_appindicator(self, AppIndicator3, Gtk) -> None:
        """Start tray using AppIndicator3 (GNOME native)."""
        print("Using AppIndicator3 for tray icon", file=sys.stderr)

        from gi.repository import GLib
        self._gtk = Gtk
        self._glib = GLib

        logo_path = get_logo_path("idle")
        icon_path = str(logo_path.absolute())
        self._appindicator_png_cache: dict[str, str] = {}

        if logo_path.exists():
            self._appindicator_png_cache["idle"] = icon_path
            print(f"AppIndicator3 icon (PNG): {icon_path}", file=sys.stderr)
        else:
            print(f"Logo not found at {icon_path}, using system icon", file=sys.stderr)
            icon_path = "audio-input-microphone"

        self.indicator = AppIndicator3.Indicator.new(
            "recordian",
            icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        assert self.indicator is not None
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Recordian")

        menu = build_appindicator_menu(self, AppIndicator3, Gtk, GLib)
        assert self.indicator is not None
        self.indicator.set_menu(menu)
        self.icon = None

        self._gtk_thread = threading.Thread(target=Gtk.main, daemon=True, name="gtk-main")
        self._gtk_thread.start()
        print("Gtk main loop started", file=sys.stderr)

    def _update_tray_menu(self) -> None:
        update_tray_menu(self)

    def _list_tray_refine_presets(self) -> list[str]:
        return list_tray_refine_presets()

    def _refresh_appindicator_preset_submenu(self) -> None:
        refresh_appindicator_preset_submenu(self, getattr(self, "_gtk", None))

    def _sync_appindicator_preset_submenu(self) -> None:
        sync_appindicator_preset_submenu(self)

    def quit(self) -> None:
        self.backend.stop()
        self.overlay.shutdown()

        if hasattr(self, '_glib'):
            def _gtk_cleanup():
                if self._gtk_settings_window is not None:
                    try:
                        self._gtk_settings_window.destroy()
                    except Exception:
                        pass
                    self._gtk_settings_window = None

                if hasattr(self, 'indicator') and self.indicator is not None:
                    try:
                        import gi
                        gi.require_version('AppIndicator3', '0.1')
                        from gi.repository import AppIndicator3
                        self.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
                    except Exception:
                        pass
                    if hasattr(self, '_gtk'):
                        try:
                            self._gtk.main_quit()
                        except Exception:
                            pass

            self._glib.idle_add(_gtk_cleanup)
        else:
            if self._gtk_settings_window is not None:
                self._gtk_settings_window = None

        self.root.quit()
        self.root.destroy()


def handle_exception(exc_type, exc_value, exc_traceback):
    """Global exception handler."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    from recordian.error_tracker import get_error_tracker
    tracker = get_error_tracker()
    if tracker:
        tracker.capture_exception(exc_value)


def main() -> None:
    sys.excepthook = handle_exception

    try:
        args = build_parser().parse_args()
        app = TrayApp(args)
        app.run()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        from recordian.error_tracker import get_error_tracker
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(e)
        raise


__all__ = [
    "build_parser",
    "RecentRunObservation",
    "UiState",
    "TrayApp",
    "handle_exception",
    "main",
]
