from __future__ import annotations

import argparse
import logging
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recordian.audio_feedback import play_sound
from recordian.backend_manager import BackendManager
from recordian.config import ConfigManager
from recordian.preset_manager import PresetManager
from recordian.runtime_config import normalize_commit_backend, normalize_notify_backend, normalize_runtime_config
from recordian.setting_effects import SettingEffect, combined_setting_effect, effect_label, effect_status_message
from recordian.voice_wake import DEFAULT_WAKE_KEYWORD_THRESHOLD, DEFAULT_WAKE_NUM_THREADS
from recordian.waveform_renderer import WaveformRenderer

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"
DEFAULT_AUTO_LEXICON_DB_PATH = "~/.config/recordian/auto_lexicon.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recordian tray GUI with waveform overlay.")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--notify-backend", choices=["none", "auto", "notify-send", "stdout"], default="auto")
    return parser


def _overlay_hide_delay_seconds(overlay: WaveformRenderer, state: str, detail: str) -> float:
    if state == "processing":
        return float(getattr(overlay, "PROCESSING_HIDE_DELAY_S", 0.50))
    if state == "error":
        return float(getattr(overlay, "ERROR_HIDE_DELAY_S", 1.55))
    if state == "idle":
        if detail.strip():
            return float(getattr(overlay, "IDLE_HIDE_DELAY_WITH_DETAIL_S", 1.10))
        return float(getattr(overlay, "IDLE_HIDE_DELAY_EMPTY_S", 0.35))
    return 0.0


def _sqlite_backup(src_path: Path, dst_path: Path) -> None:
    """Copy SQLite DB with online backup API (safer than plain file copy for live DB)."""
    src = Path(src_path).expanduser()
    dst = Path(dst_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(src)
    dst.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def _export_auto_lexicon_db(db_path: Path, export_path: Path) -> None:
    _sqlite_backup(db_path, export_path)


def _import_auto_lexicon_db(import_path: Path, db_path: Path) -> None:
    _sqlite_backup(import_path, db_path)


def _load_hotkey_default_config(*, include_sound_defaults: bool) -> dict[str, Any]:
    # Reuse the hotkey parser's defaults so the tray never invents a second set
    # of fallback values for the same runtime knobs.
    from recordian.hotkey_dictate import build_parser as build_hotkey_parser

    parser = build_hotkey_parser()
    defaults = vars(parser.parse_args([]))
    return normalize_runtime_config(
        defaults,
        include_sound_defaults=include_sound_defaults,
        allow_auto_fallback_commit=False,
    )


def _save_config_changes(
    config_path: Path,
    changes: dict[str, object],
    *,
    apply_now: bool,
    restart_callback: Callable[[], None] | None = None,
) -> tuple[SettingEffect, bool, list[str]]:
    current = ConfigManager.load(config_path)
    changed_keys = [key for key, value in changes.items() if current.get(key) != value]
    if not changed_keys:
        return SettingEffect.IMMEDIATE, False, []

    merged = dict(current)
    merged.update(changes)
    ConfigManager.save(config_path, merged)

    effect = combined_setting_effect(changed_keys)
    restarted = bool(apply_now and effect is SettingEffect.RESTART_REQUIRED and restart_callback is not None)
    if restarted and restart_callback is not None:
        restart_callback()
    return effect, restarted, changed_keys


@dataclass(slots=True)
class UiState:
    status: str = "idle"
    detail: str = "Idle"
    last_text: str = ""
    backend_running: bool = False
    last_record_ms: float = 0.0
    last_transcribe_ms: float = 0.0
    last_refine_ms: float = 0.0
    last_total_ms: float = 0.0


def get_logo_path(status: str) -> Path:
    """Get logo path based on current status."""
    # Get project root (assuming tray_gui.py is in src/recordian/)
    project_root = Path(__file__).parent.parent.parent
    assets_dir = project_root / "assets"

    logo_map = {
        "idle": "logo.png",
        "recording": "logo-recording.png",
        "processing": "logo-recording.png",
        "error": "logo-error.png",
        "stopped": "logo.png",
        "starting": "logo-warming.png",
        "warming": "logo-warming.png",
        "busy": "logo-warming.png",
    }

    logo_file = logo_map.get(status, "logo.png")
    logo_path = assets_dir / logo_file

    if not logo_path.exists():
        # Fallback to default logo
        logo_path = assets_dir / "logo.png"

    return logo_path



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
        self._appindicator_preset_submenu: Any = None
        self._appindicator_preset_items: dict[str, Any] = {}
        self._appindicator_preset_names: list[str] = []
        self._preset_menu_last_sync_ts = 0.0

    def _on_backend_state_change(self, running: bool, status: str, detail: str) -> None:
        """线程安全的状态更新回调"""
        # 使用 root.after 确保在主线程中更新状态
        def _update():
            self.state.backend_running = running
            self.state.status = status
            self.state.detail = detail
        self.root.after(0, _update)

    def run(self) -> None:
        self._start_tray()
        if not self.args.no_auto_start:
            self.backend.start()
        self.root.after(80, self._poll_events)
        self.root.mainloop()

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(80, self._poll_events)

    def _handle_event(self, event: dict[str, object]) -> None:
        et = str(event.get("event", ""))
        if et == "ready":
            self.state.backend_running = True
            self.state.status = "idle"
            self.state.detail = "Ready"
            # Suppress overlay animation during startup warmup phase to
            # avoid the "flash twice on launch" issue (PRD 4.4).
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
                latency_ms = float(event.get("latency_ms", 0.0) or 0.0)
                self.state.status = "idle"
                self.state.detail = f"Warmup ready ({latency_ms:.0f}ms)"
        elif et == "recording_started":
            self._off_cue_armed = True
            self._cancel_off_cue_timer()
            self.state.status = "recording"
            self.state.detail = "Recording..."
            self.overlay.set_state("recording", "Listening...")
            # Let overlay state update first, then play cue.
            self.root.after(0, lambda: self._play_global_cue("on"))
        elif et == "voice_wake_triggered":
            keyword = str(event.get("keyword", "")).strip()
            self.state.detail = f"已唤醒: {keyword}" if keyword else "已语音唤醒"
        elif et == "stream_partial":
            text = str(event.get("text", "")).strip()
            if text:
                self.state.last_text = text
                # Keep one-shot UX: do not show streaming words while recording.
                if self.state.status == "recording":
                    self.state.detail = "Recording..."
        elif et == "audio_level":
            self.overlay.set_level(float(event.get("level", 0.0) or 0.0))
        elif et == "processing_started":
            self.state.status = "processing"
            self.state.detail = "Processing..."
            detail = "Recognizing..."
            self.overlay.set_state("processing", detail)
            self._schedule_off_cue_from_overlay("processing", detail)
        elif et == "result":
            result = event.get("result")
            text = ""
            commit_info: dict[str, object] = {}
            if isinstance(result, dict):
                text = str(result.get("text", "")).strip()
                raw_commit = result.get("commit")
                if isinstance(raw_commit, dict):
                    commit_info = raw_commit
                # Extract performance metrics
                self.state.last_record_ms = float(result.get("record_latency_ms", 0.0) or 0.0)
                self.state.last_transcribe_ms = float(result.get("transcribe_latency_ms", 0.0) or 0.0)
                self.state.last_refine_ms = float(result.get("refine_latency_ms", 0.0) or 0.0)
                self.state.last_total_ms = self.state.last_record_ms + self.state.last_transcribe_ms + self.state.last_refine_ms
            self.state.last_text = text
            self.state.status = "idle"
            commit_backend = str(commit_info.get("backend", ""))
            commit_detail = str(commit_info.get("detail", ""))
            committed = bool(commit_info.get("committed", False))
            if text:
                print(
                    f"result text={text} committed={committed} backend={commit_backend} detail={commit_detail}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"result text=<empty> committed={committed} backend={commit_backend} detail={commit_detail}",
                    file=sys.stderr,
                    flush=True,
                )
            if text:
                if committed:
                    self.state.detail = _truncate(text, 42)
                else:
                    detail = str(commit_info.get("detail", "not_committed"))
                    self.state.detail = _truncate(f"已识别(未上屏): {text}", 42)
                    self.events.put({"event": "log", "message": f"commit_failed: {detail}"})
                detail = _truncate(text, 48)
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
            detail = _truncate(self.state.detail, 72)
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
                self.state.detail = _truncate(msg, 48)
                if msg.startswith("diag "):
                    print(msg, file=sys.stderr, flush=True)
        self._update_tray_menu()

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
        delay_s = _overlay_hide_delay_seconds(self.overlay, state, detail)
        self._schedule_off_cue(delay_s)

    def _play_global_cue(self, cue: str) -> None:
        try:
            config = ConfigManager.load(self.config_path)
            custom_path = str(config.get("sound_on_path" if cue == "on" else "sound_off_path", "")).strip()
            legacy = str(config.get("wake_beep_path", "")).strip()
            play_sound(cue=cue, custom_path=custom_path, legacy_beep_path=legacy)
        except Exception:
            # 音效失败不应影响主流程
            pass

    def toggle_text_refine(self, enabled: bool) -> None:
        """切换文本精炼；关闭时等同于快速模式。"""
        mode_text = "已启用文本精炼" if enabled else "已切换到快速模式"
        effect, restarted, _ = _save_config_changes(
            self.config_path,
            {"enable_text_refine": enabled},
            apply_now=True,
            restart_callback=lambda: self.root.after(0, self.backend.restart),
        )
        self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})

        # 显示通知反馈
        try:
            from .linux_notify import notify

            notify(effect_status_message(effect, restarted=restarted), title=f"Recordian: {mode_text}")
        except Exception:  # noqa: BLE001
            pass  # 通知失败不影响功能

        self._update_tray_menu()

    def toggle_quick_mode(self, enabled: bool) -> None:
        """兼容旧调用；快速模式开启时会关闭文本精炼。"""
        self.toggle_text_refine(not enabled)

    def toggle_voice_wake(self, enabled: bool) -> None:
        """切换语音唤醒模式"""
        mode_text = "已开启语音唤醒" if enabled else "已关闭语音唤醒"
        effect, _restarted, _ = _save_config_changes(
            self.config_path,
            {"enable_voice_wake": enabled},
            apply_now=True,
            restart_callback=lambda: self.root.after(0, self.backend.restart),
        )
        self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})
        self._update_tray_menu()

    def toggle_auto_hard_enter(self, enabled: bool) -> None:
        """切换自动硬回车"""
        mode_text = "已开启自动硬回车" if enabled else "已关闭自动硬回车"
        effect, restarted, _ = _save_config_changes(
            self.config_path,
            {"auto_hard_enter": bool(enabled)},
            apply_now=True,
            restart_callback=lambda: self.root.after(0, self.backend.restart),
        )
        self.events.put({"event": "log", "message": f"{mode_text}（{effect_label(effect)}）"})

        try:
            from .linux_notify import notify

            notify(effect_status_message(effect, restarted=restarted), title=f"Recordian: {mode_text}")
        except Exception:  # noqa: BLE001
            pass

        self._update_tray_menu()

    def copy_last_text(self) -> None:
        """复制最后识别的文本到剪贴板"""
        if not self.state.last_text:
            return
        try:
            # 使用 tkinter 剪贴板
            self.root.clipboard_clear()
            self.root.clipboard_append(self.state.last_text)
            self.root.update()
            self.events.put({"event": "log", "message": f"已复制: {self.state.last_text[:30]}..."})
        except Exception as e:
            self.events.put({"event": "log", "message": f"复制失败: {e}"})

    def switch_preset(self, preset_name: str) -> None:
        """切换文字优化 preset"""
        effect, _restarted, _ = _save_config_changes(
            self.config_path,
            {"refine_preset": preset_name},
            apply_now=True,
            restart_callback=lambda: self.root.after(0, self.backend.restart),
        )
        self.events.put({"event": "log", "message": f"已切换到 {preset_name} preset（{effect_label(effect)}）"})

        # 更新托盘菜单以反映新的选中状态
        self._update_tray_menu()

    def _list_tray_refine_presets(self) -> list[str]:
        """列出托盘菜单可用的文本精炼预设（过滤 asr-* 等非精炼预设）。"""
        preset_manager = PresetManager()
        names = [
            name for name in preset_manager.list_presets()
            if name.lower() != "readme" and not name.lower().startswith("asr-")
        ]
        builtin_order = ["default", "formal", "meeting", "summary", "technical"]
        builtin = [name for name in builtin_order if name in names]
        custom = sorted(name for name in names if name not in builtin)
        ordered = builtin + custom
        return ordered if ordered else ["default"]

    def _refresh_appindicator_preset_submenu(self) -> None:
        """重建托盘预设二级菜单，确保与 presets 目录实时联动。"""
        Gtk = getattr(self, "_gtk", None)
        preset_submenu = getattr(self, "_appindicator_preset_submenu", None)
        if Gtk is None or preset_submenu is None:
            return

        for child in list(preset_submenu.get_children()):
            preset_submenu.remove(child)

        presets = self._list_tray_refine_presets()
        config = ConfigManager.load(self.config_path)
        current_preset = str(config.get("refine_preset", "default")).strip() or "default"
        preset_labels = {
            "default": "默认",
            "formal": "正式",
            "meeting": "会议",
            "summary": "总结",
            "technical": "技术",
        }

        radio_group = None
        item_map: dict[str, Any] = {}
        for preset in presets:
            preset_item = Gtk.RadioMenuItem(group=radio_group, label=preset_labels.get(preset, preset))
            if radio_group is None:
                radio_group = preset_item
            if preset == current_preset:
                preset_item.set_active(True)
            preset_item.connect(
                "activate",
                lambda item, p=preset: self.root.after(0, lambda: self.switch_preset(p)) if item.get_active() else None,
            )
            preset_submenu.append(preset_item)
            item_map[preset] = preset_item

        self._appindicator_preset_items = item_map
        self._appindicator_preset_names = presets
        preset_submenu.show_all()

    def _sync_appindicator_preset_submenu(self) -> None:
        """同步托盘预设菜单：列表变化时重建，列表不变时仅更新选中项。"""
        now = time.monotonic()
        if now - self._preset_menu_last_sync_ts < 1.0:
            return
        self._preset_menu_last_sync_ts = now

        presets_now = self._list_tray_refine_presets()
        if presets_now != self._appindicator_preset_names:
            self._refresh_appindicator_preset_submenu()
            return

        if not self._appindicator_preset_items:
            return

        config = ConfigManager.load(self.config_path)
        current_preset = str(config.get("refine_preset", "default")).strip() or "default"
        item = self._appindicator_preset_items.get(current_preset)
        if item is not None and not bool(item.get_active()):
            item.set_active(True)


    def open_settings(self) -> None:
        current = _load_hotkey_default_config(include_sound_defaults=True)
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
        current_record_backend = str(current.get("record_backend", "auto"))
        current_record_format = str(current.get("record_format", "ogg"))
        current_refine_provider = str(current.get("refine_provider", "local"))
        current_commit_backend = normalize_commit_backend(
            current.get("commit_backend", "auto"),
            allow_auto_fallback=False,
        )
        current_enable_thinking = current.get("enable_thinking", current.get("refine_enable_thinking", False))
        current_notify_backend = normalize_notify_backend(current.get("notify_backend", "auto"))
        current["auto_hard_enter"] = bool(current.get("auto_hard_enter", False))
        current["wake_use_webrtcvad"] = bool(current.get("wake_use_webrtcvad", True))
        try:
            wake_vad_aggr = int(current.get("wake_vad_aggressiveness", 2))
        except Exception:
            wake_vad_aggr = 2
        if wake_vad_aggr not in {0, 1, 2, 3}:
            wake_vad_aggr = 2
        current["wake_vad_aggressiveness"] = wake_vad_aggr
        try:
            wake_vad_frame_ms = int(current.get("wake_vad_frame_ms", 30))
        except Exception:
            wake_vad_frame_ms = 30
        if wake_vad_frame_ms not in {10, 20, 30}:
            wake_vad_frame_ms = 30
        current["wake_vad_frame_ms"] = wake_vad_frame_ms
        try:
            wake_no_speech_timeout_s = float(current.get("wake_no_speech_timeout_s", 2.0))
        except Exception:
            wake_no_speech_timeout_s = 2.0
        current["wake_no_speech_timeout_s"] = max(0.0, wake_no_speech_timeout_s)
        try:
            wake_auto_stop_silence_s = float(current.get("wake_auto_stop_silence_s", 1.0))
        except Exception:
            wake_auto_stop_silence_s = 1.0
        current["wake_auto_stop_silence_s"] = max(0.0, wake_auto_stop_silence_s)
        try:
            wake_min_speech_s = float(current.get("wake_min_speech_s", 0.5))
        except Exception:
            wake_min_speech_s = 0.5
        current["wake_min_speech_s"] = max(0.0, wake_min_speech_s)
        try:
            wake_speech_confirm_s = float(current.get("wake_speech_confirm_s", 0.18))
        except Exception:
            wake_speech_confirm_s = 0.18
        current["wake_speech_confirm_s"] = max(0.0, wake_speech_confirm_s)
        current["wake_stats"] = bool(current.get("wake_stats", False))
        current["wake_pre_vad"] = bool(current.get("wake_pre_vad", True))
        try:
            wake_pre_vad_aggr = int(current.get("wake_pre_vad_aggressiveness", 3))
        except Exception:
            wake_pre_vad_aggr = 3
        if wake_pre_vad_aggr not in {0, 1, 2, 3}:
            wake_pre_vad_aggr = 3
        current["wake_pre_vad_aggressiveness"] = wake_pre_vad_aggr
        try:
            wake_pre_vad_frame_ms = int(current.get("wake_pre_vad_frame_ms", 30))
        except Exception:
            wake_pre_vad_frame_ms = 30
        if wake_pre_vad_frame_ms not in {10, 20, 30}:
            wake_pre_vad_frame_ms = 30
        current["wake_pre_vad_frame_ms"] = wake_pre_vad_frame_ms
        try:
            wake_pre_vad_enter_frames = int(current.get("wake_pre_vad_enter_frames", 4))
        except Exception:
            wake_pre_vad_enter_frames = 4
        current["wake_pre_vad_enter_frames"] = max(1, wake_pre_vad_enter_frames)
        try:
            wake_pre_vad_hangover_ms = int(current.get("wake_pre_vad_hangover_ms", 120))
        except Exception:
            wake_pre_vad_hangover_ms = 120
        current["wake_pre_vad_hangover_ms"] = max(0, wake_pre_vad_hangover_ms)
        try:
            wake_pre_roll_ms = int(current.get("wake_pre_roll_ms", 300))
        except Exception:
            wake_pre_roll_ms = 300
        current["wake_pre_roll_ms"] = max(0, wake_pre_roll_ms)
        try:
            wake_decode_budget_per_cycle = int(current.get("wake_decode_budget_per_cycle", 1))
        except Exception:
            wake_decode_budget_per_cycle = 1
        current["wake_decode_budget_per_cycle"] = max(1, wake_decode_budget_per_cycle)
        try:
            wake_decode_budget_per_sec = float(current.get("wake_decode_budget_per_sec", 16.0))
        except Exception:
            wake_decode_budget_per_sec = 16.0
        current["wake_decode_budget_per_sec"] = max(1.0, wake_decode_budget_per_sec)
        current["wake_auto_name_variants"] = bool(current.get("wake_auto_name_variants", True))
        current["wake_auto_prefix_variants"] = bool(current.get("wake_auto_prefix_variants", True))
        current["wake_allow_name_only"] = bool(current.get("wake_allow_name_only", True))
        current["wake_use_semantic_gate"] = bool(current.get("wake_use_semantic_gate", False))
        try:
            wake_semantic_probe_interval_s = float(current.get("wake_semantic_probe_interval_s", 0.45))
        except Exception:
            wake_semantic_probe_interval_s = 0.45
        current["wake_semantic_probe_interval_s"] = max(0.1, wake_semantic_probe_interval_s)
        try:
            wake_semantic_window_s = float(current.get("wake_semantic_window_s", 1.2))
        except Exception:
            wake_semantic_window_s = 1.2
        current["wake_semantic_window_s"] = max(0.4, wake_semantic_window_s)
        try:
            wake_semantic_end_silence_s = float(current.get("wake_semantic_end_silence_s", 1.0))
        except Exception:
            wake_semantic_end_silence_s = 1.0
        current["wake_semantic_end_silence_s"] = max(0.2, wake_semantic_end_silence_s)
        try:
            wake_semantic_min_chars = int(current.get("wake_semantic_min_chars", 1))
        except Exception:
            wake_semantic_min_chars = 1
        current["wake_semantic_min_chars"] = max(1, wake_semantic_min_chars)
        try:
            wake_semantic_timeout_ms = int(current.get("wake_semantic_timeout_ms", 1200))
        except Exception:
            wake_semantic_timeout_ms = 1200
        current["wake_semantic_timeout_ms"] = max(200, wake_semantic_timeout_ms)
        current["wake_owner_verify"] = bool(current.get("wake_owner_verify", False))
        current["wake_owner_profile"] = str(
            current.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json")
        ).strip() or "~/.config/recordian/owner_voice_profile.json"
        current["wake_owner_sample"] = str(current.get("wake_owner_sample", "")).strip()
        try:
            wake_owner_threshold = float(current.get("wake_owner_threshold", 0.72))
        except Exception:
            wake_owner_threshold = 0.72
        current["wake_owner_threshold"] = min(0.99, max(0.0, wake_owner_threshold))
        try:
            wake_owner_window_s = float(current.get("wake_owner_window_s", 1.6))
        except Exception:
            wake_owner_window_s = 1.6
        current["wake_owner_window_s"] = max(0.6, wake_owner_window_s)
        try:
            wake_owner_silence_extend_s = float(current.get("wake_owner_silence_extend_s", 0.5))
        except Exception:
            wake_owner_silence_extend_s = 0.5
        current["wake_owner_silence_extend_s"] = max(0.0, wake_owner_silence_extend_s)

        if not (hasattr(self, "_glib") and hasattr(self, "_gtk")):
            self.events.put({"event": "log", "message": "GTK 未初始化，无法打开原生设置窗口"})
            return

        self._open_settings_gtk(
            current=current,
            current_record_backend=current_record_backend,
            current_record_format=current_record_format,
            current_refine_provider=current_refine_provider,
            current_commit_backend=current_commit_backend,
            current_enable_thinking=current_enable_thinking,
            current_notify_backend=current_notify_backend,
        )

    def open_context_editor(self) -> None:
        """打开常用词编辑器"""
        if not (hasattr(self, "_glib") and hasattr(self, "_gtk")):
            self.events.put({"event": "log", "message": "GTK 未初始化，无法打开常用词编辑器"})
            return

        Gtk = self._gtk

        def _on_gtk_thread():
            # 检查是否已有窗口打开
            if hasattr(self, "_gtk_context_window") and self._gtk_context_window is not None:
                try:
                    self._gtk_context_window.present()
                    return False
                except Exception:
                    self._gtk_context_window = None

            # 加载当前配置
            try:
                current = ConfigManager.load(self.config_path)
            except Exception as e:
                self.events.put({"event": "log", "message": f"加载配置失败: {e}"})
                return False

            current_context = current.get("asr_context", "")

            # 创建窗口
            win = Gtk.Window(title="常用词管理")
            win.set_default_size(600, 400)
            win.set_position(Gtk.WindowPosition.CENTER)
            win.set_keep_above(True)
            self._gtk_context_window = win

            # 主容器
            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            root_box.set_border_width(12)
            win.add(root_box)

            # 标题
            title_label = Gtk.Label()
            title_label.set_xalign(0.0)
            title_label.set_markup("<b>常用词管理</b>")
            root_box.pack_start(title_label, False, False, 0)

            # 说明
            hint_label = Gtk.Label(label="添加常用词可以提高语音识别的准确率。多个词用逗号分隔。")
            hint_label.set_xalign(0.0)
            hint_label.set_opacity(0.75)
            hint_label.set_line_wrap(True)
            root_box.pack_start(hint_label, False, False, 0)

            # 示例
            example_label = Gtk.Label(label="示例: Recordian, Claude, Python, 张三, 李四, 机器学习")
            example_label.set_xalign(0.0)
            example_label.set_opacity(0.6)
            example_label.set_line_wrap(True)
            root_box.pack_start(example_label, False, False, 0)

            # 文本编辑区域
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroll.set_shadow_type(Gtk.ShadowType.IN)

            text_view = Gtk.TextView()
            text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            text_view.set_border_width(8)
            text_buffer = text_view.get_buffer()
            text_buffer.set_text(current_context)

            scroll.add(text_view)
            root_box.pack_start(scroll, True, True, 0)

            # 自动词库数据库导入/导出
            auto_db_raw = str(current.get("auto_lexicon_db", DEFAULT_AUTO_LEXICON_DB_PATH)).strip()
            auto_db_path = Path(auto_db_raw or DEFAULT_AUTO_LEXICON_DB_PATH).expanduser()

            db_frame = Gtk.Frame(label="自动词库数据库")
            db_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            db_box.set_border_width(8)
            db_frame.add(db_box)
            root_box.pack_start(db_frame, False, False, 0)

            db_path_label = Gtk.Label(label=f"当前数据库: {auto_db_path}")
            db_path_label.set_xalign(0.0)
            db_path_label.set_line_wrap(True)
            db_path_label.set_opacity(0.8)
            db_box.pack_start(db_path_label, False, False, 0)

            db_hint_label = Gtk.Label(label="导出可备份常用词数据库；导入后建议重启后端以立即刷新内存缓存。")
            db_hint_label.set_xalign(0.0)
            db_hint_label.set_line_wrap(True)
            db_hint_label.set_opacity(0.7)
            db_box.pack_start(db_hint_label, False, False, 0)

            db_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            db_box.pack_start(db_btn_row, False, False, 0)

            export_btn = Gtk.Button(label="导出数据库…")
            import_btn = Gtk.Button(label="导入数据库…")
            db_btn_row.pack_start(export_btn, False, False, 0)
            db_btn_row.pack_start(import_btn, False, False, 0)

            # 状态标签
            status_label = Gtk.Label()
            status_label.set_xalign(0.0)
            status_label.set_opacity(0.75)
            root_box.pack_start(status_label, False, False, 0)

            def _choose_export_path() -> Path | None:
                dialog = Gtk.FileChooserDialog(
                    title="导出常用词数据库",
                    parent=win,
                    action=Gtk.FileChooserAction.SAVE,
                )
                dialog.add_buttons(
                    Gtk.STOCK_CANCEL,
                    Gtk.ResponseType.CANCEL,
                    "导出",
                    Gtk.ResponseType.OK,
                )
                dialog.set_do_overwrite_confirmation(True)
                default_name = auto_db_path.name if auto_db_path.name else "auto_lexicon.db"
                dialog.set_current_name(default_name)
                dialog.set_current_folder(str(auto_db_path.parent))
                response = dialog.run()
                selected = Path(dialog.get_filename()).expanduser() if response == Gtk.ResponseType.OK else None
                dialog.destroy()
                return selected

            def _choose_import_path() -> Path | None:
                dialog = Gtk.FileChooserDialog(
                    title="导入常用词数据库",
                    parent=win,
                    action=Gtk.FileChooserAction.OPEN,
                )
                dialog.add_buttons(
                    Gtk.STOCK_CANCEL,
                    Gtk.ResponseType.CANCEL,
                    "导入",
                    Gtk.ResponseType.OK,
                )
                if auto_db_path.parent.exists():
                    dialog.set_current_folder(str(auto_db_path.parent))
                response = dialog.run()
                selected = Path(dialog.get_filename()).expanduser() if response == Gtk.ResponseType.OK else None
                dialog.destroy()
                return selected

            def _set_status_ok(msg: str) -> None:
                status_label.set_markup(f'<span foreground="green">✓ {msg}</span>')

            def _set_status_error(msg: str) -> None:
                status_label.set_markup(f'<span foreground="red">✗ {msg}</span>')

            def _export_db(*_args: object) -> None:
                try:
                    target = _choose_export_path()
                    if target is None:
                        return
                    _export_auto_lexicon_db(auto_db_path, target)
                    _set_status_ok(f"已导出数据库到: {target}")
                    self.events.put({"event": "log", "message": f"常用词数据库已导出: {target}"})
                except Exception as e:
                    _set_status_error(f"导出失败: {e}")

            def _import_db(*_args: object) -> None:
                try:
                    source = _choose_import_path()
                    if source is None:
                        return
                    if source == auto_db_path:
                        _set_status_error("导入源与当前数据库路径相同")
                        return
                    _import_auto_lexicon_db(source, auto_db_path)
                    _set_status_ok("已导入数据库，建议重启后端以立即刷新")
                    self.events.put({"event": "log", "message": f"常用词数据库已导入: {source} -> {auto_db_path}"})
                except Exception as e:
                    _set_status_error(f"导入失败: {e}")

            export_btn.connect("clicked", _export_db)
            import_btn.connect("clicked", _import_db)

            # 按钮区域
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            button_box.set_halign(Gtk.Align.END)
            root_box.pack_start(button_box, False, False, 0)

            # 取消按钮
            cancel_btn = Gtk.Button(label="取消")
            cancel_btn.connect("activate", lambda _: win.destroy())
            cancel_btn.connect("clicked", lambda _: win.destroy())
            button_box.pack_start(cancel_btn, False, False, 0)

            # 保存按钮
            save_btn = Gtk.Button(label="保存")
            save_btn.get_style_context().add_class("suggested-action")

            def _save_context(*_args):
                try:
                    # 获取文本
                    start_iter = text_buffer.get_start_iter()
                    end_iter = text_buffer.get_end_iter()
                    context_text = text_buffer.get_text(start_iter, end_iter, False).strip()

                    effect, restarted, _ = _save_config_changes(
                        self.config_path,
                        {"asr_context": context_text},
                        apply_now=True,
                        restart_callback=lambda: self.root.after(0, self.backend.restart),
                    )

                    status_label.set_markup(
                        f'<span foreground="green">✓ {effect_status_message(effect, restarted=restarted)}</span>'
                    )
                    self.events.put({"event": "log", "message": f"常用词已更新: {context_text[:50]}..."})

                    # 1秒后关闭窗口
                    def _close_window():
                        try:
                            win.destroy()
                        except Exception:
                            pass
                        return False

                    self._glib.timeout_add(1000, _close_window)

                except Exception as e:
                    status_label.set_markup(f'<span foreground="red">✗ 保存失败: {e}</span>')

            save_btn.connect("clicked", _save_context)
            button_box.pack_start(save_btn, False, False, 0)

            # 显示窗口
            win.connect("destroy", lambda _: setattr(self, "_gtk_context_window", None))
            win.show_all()
            win.present()
            return False

        self._glib.idle_add(_on_gtk_thread)

    def open_speaker_enrollment_wizard(self) -> None:
        """Open speaker enrollment wizard for multi-sample registration."""
        if self._gtk is None or self._glib is None:
            return

        Gtk = self._gtk

        def _on_gtk_thread() -> bool:
            dialog = Gtk.Dialog(title="声纹注册向导", transient_for=None, flags=0)
            dialog.set_modal(True)
            dialog.set_default_size(650, 500)
            dialog.set_keep_above(True)
            content = dialog.get_content_area()
            content.set_border_width(12)

            # State
            wizard_state: dict[str, object] = {
                "step": 0,  # 0=intro, 1-3=recording samples, 4=complete
                "samples": [],  # List of numpy arrays
                "recording": False,
                "record_thread": None,
                "stop_event": None,
                "chunks": [],
                "record_handler_id": None,  # Signal handler ID
            }

            # UI elements
            title_label = Gtk.Label()
            title_label.set_markup("<big><b>声纹注册向导</b></big>")
            content.pack_start(title_label, False, False, 8)

            instruction_label = Gtk.Label()
            instruction_label.set_line_wrap(True)
            instruction_label.set_xalign(0.0)
            content.pack_start(instruction_label, False, False, 8)

            # Reference text area (hidden initially)
            reference_frame = Gtk.Frame(label="参考文本")
            scroller = Gtk.ScrolledWindow()
            scroller.set_hexpand(True)
            scroller.set_vexpand(True)
            scroller.set_min_content_height(120)
            reference_text_view = Gtk.TextView()
            reference_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            reference_text_view.set_editable(False)
            reference_text_view.set_cursor_visible(False)
            reference_text_view.set_left_margin(8)
            reference_text_view.set_right_margin(8)
            scroller.add(reference_text_view)
            reference_frame.add(scroller)
            content.pack_start(reference_frame, True, True, 8)

            status_label = Gtk.Label()
            status_label.set_xalign(0.0)
            status_label.set_opacity(0.78)
            content.pack_start(status_label, False, False, 4)

            # Progress bar
            progress_label = Gtk.Label()
            progress_label.set_xalign(0.0)
            content.pack_start(progress_label, False, False, 4)

            # Buttons
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_record = Gtk.Button(label="开始录制")
            btn_next = Gtk.Button(label="下一步")
            btn_next.set_sensitive(False)
            btn_cancel = Gtk.Button(label="取消")
            button_box.pack_start(btn_record, False, False, 0)
            button_box.pack_start(btn_next, False, False, 0)
            button_box.pack_end(btn_cancel, False, False, 0)
            content.pack_start(button_box, False, False, 8)

            def _update_ui() -> None:
                step = int(wizard_state.get("step", 0))
                samples = wizard_state.get("samples", [])

                # Reference texts for each sample - designed to capture different voice characteristics
                reference_texts = [
                    # Sample 1: Natural conversational tone, mixed Chinese/English
                    "你好，我是这台电脑的主人。今天天气不错，我正在测试 Recordian 的声纹识别功能。\n"
                    "I use voice input every day for coding and writing. "
                    "希望系统能够准确识别我的声音，即使在有背景噪音的环境中。",

                    # Sample 2: Clear enunciation, technical content, numbers
                    "现在是第二段录音。请将识别结果发送到光标位置，保持自然的语速和停顿。\n"
                    "我的工作涉及编程、文档编写和邮件回复。常用的编程语言包括 Python、JavaScript 和 TypeScript。\n"
                    "今天的日期是 2025 年，版本号是 3.14.159。",

                    # Sample 3: Varied pitch and emotion, questions and statements
                    "这是最后一段测试录音。声纹识别技术真的很神奇！它能区分不同人的声音特征吗？\n"
                    "当然可以。通过分析音色、音调、语速和发音习惯，系统可以建立独特的声纹模型。\n"
                    "完成注册后，只有我的声音才能激活语音输入功能。"
                ]

                if step == 0:
                    instruction_label.set_text(
                        "欢迎使用声纹注册向导！\n\n"
                        "您需要录制3个语音样本来创建声纹档案。\n"
                        "每个样本建议录制5-10秒，请在安静环境中清晰朗读参考文本。\n\n"
                        "点击「下一步」开始。"
                    )
                    reference_frame.hide()
                    status_label.set_text("准备就绪")
                    progress_label.set_text("进度: 0/3 样本")
                    btn_record.set_visible(False)
                    btn_next.set_label("下一步")
                    btn_next.set_sensitive(True)
                elif 1 <= step <= 3:
                    instruction_label.set_text(
                        f"样本 {step}/3\n\n"
                        "请按照下方参考文本朗读并录制。\n"
                        "点击「开始录制」，朗读完成后点击「停止录制」。"
                    )
                    # Show reference text
                    reference_text_view.get_buffer().set_text(reference_texts[step - 1])
                    reference_frame.show()
                    reference_frame.show_all()
                    status_label.set_text("等待录制")
                    progress_label.set_text(f"进度: {len(samples)}/3 样本")
                    btn_record.set_visible(True)
                    btn_record.set_label("开始录制")
                    btn_record.set_sensitive(not wizard_state.get("recording", False))
                    btn_next.set_label("下一步")
                    btn_next.set_sensitive(len(samples) >= step)
                elif step == 4:
                    instruction_label.set_text(
                        "注册完成！\n\n"
                        "已成功录制3个样本并创建声纹档案。\n"
                        "声纹验证功能已自动启用。"
                    )
                    reference_frame.hide()
                    status_label.set_text("注册成功")
                    progress_label.set_text("进度: 3/3 样本 ✓")
                    btn_record.set_visible(False)
                    btn_next.set_label("完成")
                    btn_next.set_sensitive(True)

            def _start_recording(*_args: object) -> None:
                if wizard_state.get("recording"):
                    return

                config = ConfigManager.load(self.config_path)
                sample_rate = int(config.get("wake_sample_rate", 16000))
                sample_rate = max(8000, min(48000, sample_rate))

                stop_event = threading.Event()
                chunks: list[object] = []
                wizard_state["stop_event"] = stop_event
                wizard_state["chunks"] = chunks
                wizard_state["recording"] = True

                def _worker() -> None:
                    try:
                        import numpy as np
                        import sounddevice as sd

                        with sd.InputStream(
                            channels=1,
                            samplerate=sample_rate,
                            dtype="float32",
                            blocksize=1024,
                        ) as stream:
                            while not stop_event.is_set():
                                data, _ = stream.read(1024)
                                frame = np.ascontiguousarray(data.reshape(-1), dtype=np.float32)
                                chunks.append(frame.copy())
                    except Exception as exc:  # noqa: BLE001
                        wizard_state["error"] = f"{type(exc).__name__}: {exc}"

                thread = threading.Thread(target=_worker, daemon=True)
                wizard_state["record_thread"] = thread
                thread.start()

                btn_record.set_label("停止录制")
                btn_record.set_sensitive(True)
                status_label.set_text("录制中... 请清晰朗读")

                def _on_record_click(*_args: object) -> None:
                    _stop_recording()

                # Disconnect old handler
                handler_id = wizard_state.get("record_handler_id")
                if handler_id is not None:
                    btn_record.disconnect(handler_id)

                # Connect new handler
                wizard_state["record_handler_id"] = btn_record.connect("clicked", _on_record_click)

            def _stop_recording() -> None:
                stop_event = wizard_state.get("stop_event")
                if isinstance(stop_event, threading.Event):
                    stop_event.set()
                thread = wizard_state.get("record_thread")
                if isinstance(thread, threading.Thread):
                    thread.join(timeout=3.0)
                wizard_state["recording"] = False
                wizard_state["record_thread"] = None

                # Process recorded audio
                chunks_obj = wizard_state.get("chunks", [])
                if not chunks_obj:
                    status_label.set_text("未采集到音频，请重试")
                    btn_record.set_label("开始录制")
                    # Reconnect start handler
                    handler_id = wizard_state.get("record_handler_id")
                    if handler_id is not None:
                        btn_record.disconnect(handler_id)
                    wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                    return

                try:
                    import numpy as np
                    samples = np.concatenate(chunks_obj).astype(np.float32)

                    if samples.size < 16000:  # Less than 1 second at 16kHz
                        status_label.set_text("录音太短，请至少录制1秒")
                        btn_record.set_label("开始录制")
                        # Reconnect start handler
                        handler_id = wizard_state.get("record_handler_id")
                        if handler_id is not None:
                            btn_record.disconnect(handler_id)
                        wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                        return

                    # Quality check using speaker_verify
                    try:
                        from recordian.speaker_verify import _assess_sample_quality
                        config = ConfigManager.load(self.config_path)
                        sample_rate = int(config.get("wake_sample_rate", 16000))
                        quality_ok, reason = _assess_sample_quality(samples, sample_rate)

                        if not quality_ok:
                            status_label.set_text(f"样本质量不足: {reason}，请重新录制")
                            btn_record.set_label("开始录制")
                            # Reconnect start handler
                            handler_id = wizard_state.get("record_handler_id")
                            if handler_id is not None:
                                btn_record.disconnect(handler_id)
                            wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                            return
                    except Exception:  # noqa: BLE001
                        pass  # Skip quality check if not available

                    # Save sample
                    samples_list = wizard_state.get("samples")
                    if not isinstance(samples_list, list):
                        samples_list = []
                        wizard_state["samples"] = samples_list
                    samples_list.append(samples)
                    step = int(wizard_state.get("step", 0))
                    status_label.set_text(f"样本 {step} 录制成功 ✓")
                    btn_record.set_label("开始录制")
                    # Reconnect start handler
                    handler_id = wizard_state.get("record_handler_id")
                    if handler_id is not None:
                        btn_record.disconnect(handler_id)
                    wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                    _update_ui()

                except Exception as exc:  # noqa: BLE001
                    status_label.set_text(f"处理失败: {type(exc).__name__}: {exc}")
                    btn_record.set_label("开始录制")
                    # Reconnect start handler
                    handler_id = wizard_state.get("record_handler_id")
                    if handler_id is not None:
                        btn_record.disconnect(handler_id)
                    wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)

            def _next_step(*_args: object) -> None:
                step = int(wizard_state.get("step", 0))

                if step == 4:
                    # Complete
                    dialog.destroy()
                    return

                wizard_state["step"] = step + 1
                wizard_state["chunks"] = []

                if wizard_state["step"] == 4:
                    # Save profile
                    _save_profile()

                _update_ui()

            def _save_profile() -> None:
                try:
                    import numpy as np

                    from recordian.speaker_verify import enroll_speaker_profile

                    config = ConfigManager.load(self.config_path)
                    sample_rate = int(config.get("wake_sample_rate", 16000))
                    profile_path = Path(config.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json")).expanduser()

                    samples = wizard_state.get("samples", [])
                    if len(samples) < 3:
                        status_label.set_text("样本数量不足")
                        return

                    # Save samples as WAV files
                    sample_paths = []
                    for i, sample in enumerate(samples):
                        sample_path = profile_path.parent / f"owner_sample_{i+1}.wav"
                        sample_path.parent.mkdir(parents=True, exist_ok=True)

                        pcm = (np.clip(sample, -1.0, 1.0) * 32767.0).astype("<i2")
                        with wave.open(str(sample_path), "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(sample_rate)
                            wf.writeframes(pcm.tobytes())
                        sample_paths.append(sample_path)

                    # Create profile
                    enroll_speaker_profile(
                        sample_paths=sample_paths,
                        profile_path=profile_path,
                        target_rate=sample_rate,
                    )

                    effect, restarted, _ = _save_config_changes(
                        self.config_path,
                        {
                            "wake_owner_verify": True,
                            "wake_owner_profile": str(profile_path),
                        },
                        apply_now=True,
                        restart_callback=lambda: self.root.after(0, self.backend.restart),
                    )
                    status_label.set_text(
                        f"声纹档案已保存: {profile_path}；{effect_status_message(effect, restarted=restarted)}"
                    )

                except Exception as exc:  # noqa: BLE001
                    status_label.set_text(f"保存失败: {type(exc).__name__}: {exc}")

            def _cancel(*_args: object) -> None:
                if wizard_state.get("recording"):
                    _stop_recording()
                dialog.destroy()

            wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
            btn_next.connect("clicked", _next_step)
            btn_cancel.connect("clicked", _cancel)
            dialog.connect("delete-event", lambda *_: (_cancel(), False)[1])

            _update_ui()
            dialog.show_all()
            return False

        self._glib.idle_add(_on_gtk_thread)

    def _open_settings_gtk(
        self,
        *,
        current: dict[str, Any],
        current_record_backend: str,
        current_record_format: str,
        current_refine_provider: str,
        current_commit_backend: str,
        current_enable_thinking: object,
        current_notify_backend: str,
    ) -> None:
        Gtk = self._gtk

        def _coerce_bool(value: object, *, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return _parse_bool(value, default=default)
            return default

        def _on_gtk_thread() -> bool:
            if self._gtk_settings_window is not None:
                try:
                    self._gtk_settings_window.present()
                    return False
                except Exception:
                    self._gtk_settings_window = None

            win = Gtk.Window(title="Recordian 设置")
            win.set_default_size(900, 760)
            win.set_position(Gtk.WindowPosition.CENTER)
            win.set_keep_above(True)
            self._gtk_settings_window = win

            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            root_box.set_border_width(12)
            win.add(root_box)

            title_label = Gtk.Label(label="Recordian 设置")
            title_label.set_xalign(0.0)
            title_label.set_markup("<b>Recordian 设置</b>")
            root_box.pack_start(title_label, False, False, 0)

            config_label = Gtk.Label(label=f"配置文件: {self.config_path}")
            config_label.set_xalign(0.0)
            config_label.set_opacity(0.75)
            root_box.pack_start(config_label, False, False, 0)

            notebook = Gtk.Notebook()
            root_box.pack_start(notebook, True, True, 0)

            entries: dict[str, tuple[str, Any]] = {}
            status_label_ref: dict[str, Any] = {"widget": None}
            try:
                from gi.repository import Gdk  # type: ignore
            except Exception:
                Gdk = None

            def _create_tab(name: str) -> Gtk.Box:
                page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                page.set_border_width(10)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                scroll.add(page)
                notebook.append_page(scroll, Gtk.Label(label=name))
                return page

            def _create_section(parent: Gtk.Box, title: str) -> Gtk.Grid:
                frame = Gtk.Frame(label=title)
                frame.set_margin_top(4)
                frame.set_margin_bottom(6)
                grid = Gtk.Grid()
                grid.set_border_width(10)
                grid.set_column_spacing(12)
                grid.set_row_spacing(6)
                frame.add(grid)
                parent.pack_start(frame, False, False, 0)
                return grid

            def _add_field(
                grid: Gtk.Grid,
                row: int,
                *,
                key: str,
                label: str,
                value: object,
                kind: str = "entry",
                options: tuple[str, ...] = (),
                hint: str = "",
                default_bool: bool = False,
                secret: bool = False,
            ) -> int:
                if kind == "bool":
                    row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                    row_box.set_hexpand(True)
                    row_box.set_halign(Gtk.Align.FILL)

                    left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                    left_box.set_hexpand(True)
                    title_label = Gtk.Label(label=label)
                    title_label.set_xalign(0.0)
                    left_box.pack_start(title_label, False, False, 0)
                    if hint:
                        hint_label = Gtk.Label(label=hint)
                        hint_label.set_xalign(0.0)
                        hint_label.set_opacity(0.75)
                        left_box.pack_start(hint_label, False, False, 0)
                    row_box.pack_start(left_box, True, True, 0)

                    widget = Gtk.Switch()
                    widget.set_active(_coerce_bool(value, default=default_bool))
                    widget.set_halign(Gtk.Align.START)
                    widget.set_valign(Gtk.Align.CENTER)
                    widget.set_size_request(44, 24)

                    state_label = Gtk.Label()
                    state_label.set_xalign(0.0)
                    state_label.set_opacity(0.75)
                    state_label.set_text("开" if widget.get_active() else "关")

                    def _sync_switch_label(sw: Any, *_args: object) -> None:
                        state_label.set_text("开" if sw.get_active() else "关")

                    widget.connect("notify::active", _sync_switch_label)

                    switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    switch_box.set_halign(Gtk.Align.END)
                    switch_box.set_valign(Gtk.Align.CENTER)
                    switch_box.pack_start(widget, False, False, 0)
                    switch_box.pack_start(state_label, False, False, 0)
                    row_box.pack_end(switch_box, False, False, 0)

                    grid.attach(row_box, 0, row, 2, 1)
                    entries[key] = ("bool", widget)
                    return row + 1

                label_widget = Gtk.Label(label=label)
                label_widget.set_xalign(0.0)
                label_widget.set_yalign(0.0)
                grid.attach(label_widget, 0, row, 1, 1)
                next_row = row + 1
                if kind == "combo":
                    widget = Gtk.ComboBoxText()
                    selected = str(value)
                    options_list = list(options)
                    if selected and selected not in options_list:
                        options_list.append(selected)
                    for option in options_list:
                        widget.append_text(option)
                    if options_list:
                        try:
                            active_idx = options_list.index(selected)
                        except ValueError:
                            active_idx = 0
                        widget.set_active(active_idx)
                    grid.attach(widget, 1, row, 1, 1)
                    entries[key] = ("combo", widget)
                else:
                    widget = Gtk.Entry()
                    widget.set_text(str(value))
                    widget.set_hexpand(True)
                    if secret:
                        widget.set_visibility(False)
                        widget.set_invisible_char("●")
                    if key in HOTKEY_CAPTURE_FIELDS and Gdk is not None:
                        widget.set_placeholder_text("点击后按组合键自动识别")

                        def _on_hotkey_press(entry: Any, event: object, field_label: str = label) -> bool:
                            keyval = getattr(event, "keyval", None)
                            raw_name = Gdk.keyval_name(keyval) if keyval is not None else ""
                            key_name = (raw_name or "").lower()
                            if key_name in {"tab", "iso_left_tab"}:
                                return False
                            if key_name in {"backspace", "delete"}:
                                entry.set_text("")
                                status_widget = status_label_ref.get("widget")
                                if status_widget is not None:
                                    status_widget.set_text(f"{field_label} 已清空")
                                return True
                            spec = _build_gtk_hotkey_spec(event, Gdk)
                            if spec:
                                entry.set_text(spec)
                                status_widget = status_label_ref.get("widget")
                                if status_widget is not None:
                                    status_widget.set_text(f"{field_label} 已更新: {spec}")
                            return True

                        widget.connect("key-press-event", _on_hotkey_press)
                    grid.attach(widget, 1, row, 1, 1)
                    entries[key] = ("entry", widget)
                if hint:
                    hint_label = Gtk.Label(label=hint)
                    hint_label.set_xalign(0.0)
                    hint_label.set_opacity(0.75)
                    grid.attach(hint_label, 1, next_row, 1, 1)
                    next_row += 1
                return next_row

            preset_manager = PresetManager()

            def _list_editable_refine_presets() -> list[str]:
                if not preset_manager.presets_dir.exists():
                    return []
                names: list[str] = []
                for p in sorted(preset_manager.presets_dir.glob("*.md")):
                    stem = p.stem
                    if stem.lower() == "readme":
                        continue
                    # ASR Context 预设单独管理，不纳入文本精炼预设编辑页。
                    if stem.startswith("asr-"):
                        continue
                    names.append(stem)
                return names

            tab_basic = _create_tab("基础")
            tab_asr = _create_tab("ASR")
            tab_refine = _create_tab("文本精炼")
            tab_presets = _create_tab("预设管理")
            tab_wake = _create_tab("语音唤醒")
            tab_advanced = _create_tab("高级")

            sec_hotkey = _create_section(tab_basic, "热键与触发")
            row = 0
            row = _add_field(
                sec_hotkey,
                row,
                key="hotkey",
                label="触发热键",
                value=current.get("hotkey", "<ctrl_r>"),
                hint="点击输入框后直接按组合键自动识别（Delete/Backspace 可清空）",
            )
            row = _add_field(
                sec_hotkey,
                row,
                key="stop_hotkey",
                label="停止热键",
                value=current.get("stop_hotkey", ""),
                hint="留空表示使用默认停止逻辑",
            )
            row = _add_field(sec_hotkey, row, key="toggle_hotkey", label="切换热键", value=current.get("toggle_hotkey", ""))
            row = _add_field(
                sec_hotkey,
                row,
                key="trigger_mode",
                label="触发模式",
                value=current.get("trigger_mode", "ptt"),
                kind="combo",
                options=("ptt", "toggle", "oneshot"),
            )
            _add_field(sec_hotkey, row, key="cooldown_ms", label="冷却时间 (ms)", value=current.get("cooldown_ms", 300))

            sec_record = _create_section(tab_basic, "录音")
            row = 0
            row = _add_field(sec_record, row, key="duration", label="录音时长 (s)", value=current.get("duration", 4.0))
            row = _add_field(
                sec_record,
                row,
                key="record_backend",
                label="录音后端",
                value=current_record_backend,
                kind="combo",
                options=("auto", "ffmpeg-pulse", "arecord"),
            )
            row = _add_field(
                sec_record,
                row,
                key="record_format",
                label="录音格式",
                value=current_record_format,
                kind="combo",
                options=("ogg", "wav"),
            )
            row = _add_field(sec_record, row, key="sample_rate", label="采样率", value=current.get("sample_rate", 16000))
            row = _add_field(sec_record, row, key="channels", label="声道数", value=current.get("channels", 1))
            _add_field(sec_record, row, key="input_device", label="输入设备", value=current.get("input_device", "default"))

            sec_asr = _create_section(tab_asr, "识别配置")
            row = 0
            row = _add_field(
                sec_asr,
                row,
                key="asr_provider",
                label="ASR Provider",
                value=current.get("asr_provider", "qwen-asr"),
                kind="combo",
                options=("qwen-asr", "http-cloud"),
            )
            row = _add_field(
                sec_asr,
                row,
                key="qwen_model",
                label="ASR 模型（路径或模型ID）",
                value=current.get("qwen_model", ""),
                hint="qwen-asr: 本地模型路径；http-cloud: 远端服务的 model 名称（如 Qwen/Qwen3-ASR-0.6B）",
            )
            row = _add_field(
                sec_asr,
                row,
                key="qwen_language",
                label="Qwen 语言",
                value=current.get("qwen_language", "Chinese"),
                kind="combo",
                options=("Chinese", "English", "auto"),
            )
            row = _add_field(sec_asr, row, key="qwen_max_new_tokens", label="Qwen Max Tokens", value=current.get("qwen_max_new_tokens", 1024))
            row = _add_field(
                sec_asr,
                row,
                key="asr_endpoint",
                label="HTTP ASR Endpoint",
                value=current.get("asr_endpoint", "http://127.0.0.1:8000/v1/audio/transcriptions"),
                hint="仅 asr_provider=http-cloud 时生效。vLLM/OpenAI 兼容接口示例：/v1/audio/transcriptions",
            )
            row = _add_field(
                sec_asr,
                row,
                key="asr_api_key",
                label="HTTP ASR API Key",
                value=current.get("asr_api_key", ""),
                hint="仅 asr_provider=http-cloud 时生效（留空表示不带鉴权头）",
                secret=True,
            )
            row = _add_field(sec_asr, row, key="asr_timeout_s", label="HTTP ASR Timeout (s)", value=current.get("asr_timeout_s", 30.0))
            row = _add_field(
                sec_asr,
                row,
                key="asr_context_preset",
                label="ASR Context 预设",
                value=current.get("asr_context_preset", ""),
                hint="留空或填写: default/formal/meeting/technical/simple",
            )
            row = _add_field(sec_asr, row, key="asr_context", label="ASR Context 自定义", value=current.get("asr_context", ""))
            _add_field(
                sec_asr,
                row,
                key="device",
                label="计算设备",
                value=current.get("device", "cuda"),
                kind="combo",
                options=("cuda", "cpu", "auto"),
            )

            sec_refine = _create_section(tab_refine, "文本精炼")
            row = 0
            current_refine_preset_name = {"value": str(current.get("refine_preset", "default")).strip() or "default"}

            def _get_current_refine_preset_name() -> str:
                return str(current_refine_preset_name["value"]).strip() or "default"

            def _set_current_refine_preset_name(name: str) -> None:
                current_refine_preset_name["value"] = str(name).strip() or "default"
                if current_preset_value_label is not None:
                    current_preset_value_label.set_text(_get_current_refine_preset_name())

            row = _add_field(
                sec_refine,
                row,
                key="enable_text_refine",
                label="启用文本精炼",
                value=current.get("enable_text_refine", False),
                kind="bool",
                default_bool=False,
                hint="关闭后直接输出识别结果，等同于托盘里的快速模式。",
            )
            row = _add_field(
                sec_refine,
                row,
                key="refine_provider",
                label="精炼 Provider",
                value=current_refine_provider,
                kind="combo",
                options=("local", "cloud", "llamacpp"),
            )
            refine_preset_label = Gtk.Label(label="当前精炼预设")
            refine_preset_label.set_xalign(0.0)
            sec_refine.attach(refine_preset_label, 0, row, 1, 1)

            current_preset_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            current_preset_box.set_hexpand(True)
            current_preset_value_label = Gtk.Label(label=_get_current_refine_preset_name())
            current_preset_value_label.set_xalign(0.0)
            current_preset_box.pack_start(current_preset_value_label, False, False, 0)
            current_preset_hint = Gtk.Label(label="切换请到“预设管理”页，或直接使用托盘菜单。")
            current_preset_hint.set_xalign(0.0)
            current_preset_hint.set_opacity(0.75)
            current_preset_box.pack_start(current_preset_hint, False, False, 0)
            sec_refine.attach(current_preset_box, 1, row, 1, 1)
            row += 1
            row = _add_field(
                sec_refine,
                row,
                key="refine_model",
                label="精炼模型路径",
                value=current.get("refine_model", ""),
                hint="local=HF 模型路径; llamacpp=GGUF 文件路径",
            )
            row = _add_field(
                sec_refine,
                row,
                key="refine_device",
                label="精炼设备",
                value=current.get("refine_device", "cuda"),
                kind="combo",
                options=("cuda", "cpu", "auto"),
            )
            row = _add_field(sec_refine, row, key="refine_n_gpu_layers", label="llama.cpp GPU 层数", value=current.get("refine_n_gpu_layers", -1))
            row = _add_field(sec_refine, row, key="refine_max_tokens", label="精炼 Max Tokens", value=current.get("refine_max_tokens", 512))
            row = _add_field(
                sec_refine,
                row,
                key="enable_thinking",
                label="启用 Thinking 模式",
                value=current_enable_thinking,
                kind="bool",
                default_bool=False,
            )
            row = _add_field(sec_refine, row, key="refine_api_base", label="云端 API Base", value=current.get("refine_api_base", ""))
            row = _add_field(sec_refine, row, key="refine_api_key", label="云端 API Key", value=current.get("refine_api_key", ""), secret=True)
            _add_field(sec_refine, row, key="refine_api_model", label="云端 API 模型", value=current.get("refine_api_model", ""))

            sec_presets = _create_section(tab_presets, "文本精炼预设管理")
            preset_row = 0

            preset_select_label = Gtk.Label(label="编辑预设")
            preset_select_label.set_xalign(0.0)
            sec_presets.attach(preset_select_label, 0, preset_row, 1, 1)

            preset_select_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            preset_combo = Gtk.ComboBoxText()
            preset_combo.set_hexpand(False)
            btn_set_current = Gtk.Button(label="设为当前")
            preset_select_box.pack_start(preset_combo, False, False, 0)
            preset_select_box.pack_start(btn_set_current, False, False, 0)
            sec_presets.attach(preset_select_box, 1, preset_row, 1, 1)
            preset_row += 1

            new_name_label = Gtk.Label(label="新建预设")
            new_name_label.set_xalign(0.0)
            sec_presets.attach(new_name_label, 0, preset_row, 1, 1)

            new_name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            preset_name_entry = Gtk.Entry()
            preset_name_entry.set_placeholder_text("仅英文/数字/_/-，例如：my-note")
            preset_name_entry.set_hexpand(True)
            btn_create = Gtk.Button(label="新建")
            new_name_box.pack_start(preset_name_entry, True, True, 0)
            new_name_box.pack_start(btn_create, False, False, 0)
            sec_presets.attach(new_name_box, 1, preset_row, 1, 1)
            preset_row += 1

            editor_label = Gtk.Label(label="预设内容")
            editor_label.set_xalign(0.0)
            sec_presets.attach(editor_label, 0, preset_row, 1, 1)

            editor_scroll = Gtk.ScrolledWindow()
            editor_scroll.set_hexpand(True)
            editor_scroll.set_vexpand(True)
            editor_scroll.set_min_content_height(320)
            preset_text = Gtk.TextView()
            preset_text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            preset_text.set_monospace(True)
            preset_buffer = preset_text.get_buffer()
            editor_scroll.add(preset_text)
            sec_presets.attach(editor_scroll, 1, preset_row, 1, 1)
            preset_row += 1

            action_label = Gtk.Label(label="操作")
            action_label.set_xalign(0.0)
            sec_presets.attach(action_label, 0, preset_row, 1, 1)

            action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_save_preset = Gtk.Button(label="保存预设")
            btn_delete_preset = Gtk.Button(label="删除预设")
            btn_refresh_preset = Gtk.Button(label="刷新列表")
            action_box.pack_start(btn_save_preset, False, False, 0)
            action_box.pack_start(btn_delete_preset, False, False, 0)
            action_box.pack_start(btn_refresh_preset, False, False, 0)
            sec_presets.attach(action_box, 1, preset_row, 1, 1)

            sec_advanced = _create_section(tab_advanced, "上屏与运行")
            row = 0
            row = _add_field(
                sec_advanced,
                row,
                key="commit_backend",
                label="上屏后端",
                value=current_commit_backend,
                kind="combo",
                options=("auto", "wtype", "xdotool", "xdotool-clipboard", "stdout", "none"),
                hint="X11 + Electron 建议 xdotool-clipboard",
            )
            row = _add_field(
                sec_advanced,
                row,
                key="auto_hard_enter",
                label="自动硬回车",
                value=current.get("auto_hard_enter", False),
                kind="bool",
                default_bool=False,
                hint="识别文本上屏后，额外发送一次 Enter 键",
            )
            row = _add_field(
                sec_advanced,
                row,
                key="warmup",
                label="启动时预热",
                value=current.get("warmup", True),
                kind="bool",
                default_bool=True,
            )
            row = _add_field(
                sec_advanced,
                row,
                key="debug_diagnostics",
                label="调试诊断",
                value=current.get("debug_diagnostics", False),
                kind="bool",
                default_bool=False,
            )
            _add_field(
                sec_advanced,
                row,
                key="notify_backend",
                label="通知后端",
                value=current_notify_backend,
                kind="combo",
                options=("auto", "notify-send", "stdout", "none"),
            )

            sec_wake_main = _create_section(tab_wake, "基础设置")
            row = 0
            row = _add_field(
                sec_wake_main,
                row,
                key="enable_voice_wake",
                label="启用语音唤醒",
                value=current.get("enable_voice_wake", False),
                kind="bool",
                default_bool=False,
                hint="开启后后台常驻监听，热键与语音可共存",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_prefix",
                label="唤醒前缀（逗号分隔）",
                value=",".join(current.get("wake_prefix", ["嗨", "嘿"])),
                hint="例如：嗨,嘿",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_name",
                label="唤醒名字（逗号分隔）",
                value=",".join(current.get("wake_name", ["小二"])),
                hint="例如：小二,乐乐,小三",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_owner_verify",
                label="仅主人声音可唤醒",
                value=current.get("wake_owner_verify", False),
                kind="bool",
                default_bool=False,
                hint="命中唤醒词后，再做一次声纹校验，减少误触发",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_owner_sample",
                label="主人声音样本 WAV（可选）",
                value=current.get("wake_owner_sample", ""),
                hint="首次可填 8~20 秒样本，缺少 profile 时自动提取",
            )
            owner_button_label = Gtk.Label(label="主人声音采集")
            owner_button_label.set_xalign(0.0)
            sec_wake_main.attach(owner_button_label, 0, row, 1, 1)
            owner_button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            owner_button_box.set_hexpand(True)
            btn_record_owner_sample = Gtk.Button(label="录制主人样本…")
            btn_record_owner_sample.set_halign(Gtk.Align.START)
            owner_button_box.pack_start(btn_record_owner_sample, False, False, 0)
            owner_button_hint = Gtk.Label(label="点击后弹出参考文本，按提示录制并保存为参考声音。")
            owner_button_hint.set_xalign(0.0)
            owner_button_hint.set_opacity(0.75)
            owner_button_box.pack_start(owner_button_hint, False, False, 0)
            sec_wake_main.attach(owner_button_box, 1, row, 1, 1)
            row += 1
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_cooldown_s",
                label="唤醒冷却时间 (s)",
                value=current.get("wake_cooldown_s", 3.0),
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_auto_stop_silence_s",
                label="静音自动结束 (s)",
                value=current.get("wake_auto_stop_silence_s", 1.0),
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_min_speech_s",
                label="最短说话时长 (s)",
                value=current.get("wake_min_speech_s", 0.5),
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_use_webrtcvad",
                label="使用 WebRTC VAD",
                value=current.get("wake_use_webrtcvad", True),
                kind="bool",
                default_bool=True,
                hint="语音/非语音判定更稳，建议开启",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_vad_aggressiveness",
                label="VAD 灵敏度",
                value=str(current.get("wake_vad_aggressiveness", 2)),
                kind="combo",
                options=("0", "1", "2", "3"),
                hint="3 更严格（更抗噪）",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_vad_frame_ms",
                label="VAD 帧长 (ms)",
                value=str(current.get("wake_vad_frame_ms", 30)),
                kind="combo",
                options=("10", "20", "30"),
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="wake_no_speech_timeout_s",
                label="唤醒后未开口超时 (s)",
                value=current.get("wake_no_speech_timeout_s", 2.0),
                hint="超时自动结束本次录音",
            )
            row = _add_field(
                sec_wake_main,
                row,
                key="sound_on_path",
                label="开始音效路径",
                value=current.get("sound_on_path", ""),
                hint="录音启动时播放（支持 mp3/wav）",
            )
            _add_field(
                sec_wake_main,
                row,
                key="sound_off_path",
                label="结束音效路径",
                value=current.get("sound_off_path", ""),
                hint="录音结束时播放（支持 mp3/wav）",
            )

            wake_model_dir = Path(__file__).parent.parent.parent / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
            sec_wake_model = _create_section(tab_wake, "模型与阈值")
            row = 0
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_encoder",
                label="Encoder ONNX",
                value=current.get("wake_encoder", str(wake_model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx")),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_decoder",
                label="Decoder ONNX",
                value=current.get("wake_decoder", str(wake_model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx")),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_joiner",
                label="Joiner ONNX",
                value=current.get("wake_joiner", str(wake_model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx")),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_tokens",
                label="Tokens 文件",
                value=current.get("wake_tokens", str(wake_model_dir / "tokens.txt")),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_keywords_file",
                label="关键词文件（可选）",
                value=current.get("wake_keywords_file", ""),
                hint="留空自动由前缀+名字生成",
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_tokens_type",
                label="Tokens 类型",
                value=current.get("wake_tokens_type", "ppinyin"),
                kind="combo",
                options=("ppinyin", "cjkchar", "bpe", "fpinyin"),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_provider",
                label="推理 Provider",
                value=current.get("wake_provider", "cpu"),
                kind="combo",
                options=("cpu", "cuda"),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_num_threads",
                label="线程数",
                value=current.get("wake_num_threads", DEFAULT_WAKE_NUM_THREADS),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_sample_rate",
                label="采样率",
                value=current.get("wake_sample_rate", 16000),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_keyword_score",
                label="关键词分数",
                value=current.get("wake_keyword_score", 1.5),
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_owner_threshold",
                label="主人声纹阈值",
                value=current.get("wake_owner_threshold", 0.72),
                hint="0~1，越高越严格（建议 0.68~0.80）",
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_owner_window_s",
                label="声纹分析窗口 (s)",
                value=current.get("wake_owner_window_s", 1.6),
                hint="唤醒后回看最近音频时长",
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_owner_silence_extend_s",
                label="主人静音延长 (s)",
                value=current.get("wake_owner_silence_extend_s", 0.5),
                hint="识别为主人时延长静音阈值，避免停顿被打断",
            )
            row = _add_field(
                sec_wake_model,
                row,
                key="wake_owner_profile",
                label="主人声纹特征文件",
                value=current.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json"),
                hint="JSON 文件路径，可备份/迁移",
            )
            _add_field(
                sec_wake_model,
                row,
                key="wake_keyword_threshold",
                label="关键词阈值",
                value=current.get("wake_keyword_threshold", DEFAULT_WAKE_KEYWORD_THRESHOLD),
            )

            sec_wake_advanced = _create_section(tab_wake, "高级调优")
            row = 0
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_stats",
                label="输出唤醒统计",
                value=current.get("wake_stats", False),
                kind="bool",
                default_bool=False,
                hint="周期输出 voice_wake_stats 事件（排查 CPU 用）",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_pre_vad",
                label="待机 pre-VAD 门控",
                value=current.get("wake_pre_vad", True),
                kind="bool",
                default_bool=True,
                hint="先过 VAD 再进入 KWS 解码，通常可降低 CPU",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_pre_vad_aggressiveness",
                label="pre-VAD 灵敏度",
                value=str(current.get("wake_pre_vad_aggressiveness", 3)),
                kind="combo",
                options=("0", "1", "2", "3"),
                hint="3 更严格，背景噪声下更稳",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_pre_vad_frame_ms",
                label="pre-VAD 帧长 (ms)",
                value=str(current.get("wake_pre_vad_frame_ms", 30)),
                kind="combo",
                options=("10", "20", "30"),
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_pre_vad_enter_frames",
                label="pre-VAD 进入帧数",
                value=current.get("wake_pre_vad_enter_frames", 4),
                hint="连续判定为语音多少帧后，打开 KWS 门",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_pre_vad_hangover_ms",
                label="pre-VAD 挂起时长 (ms)",
                value=current.get("wake_pre_vad_hangover_ms", 120),
                hint="最后一帧语音后，额外保持门打开的时长",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_pre_roll_ms",
                label="pre-roll (ms)",
                value=current.get("wake_pre_roll_ms", 300),
                hint="门打开时回放前序音频长度，减少截断漏检",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_decode_budget_per_cycle",
                label="每周期解码预算",
                value=current.get("wake_decode_budget_per_cycle", 1),
                hint="单个音频读取周期内最多解码次数",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_decode_budget_per_sec",
                label="每秒解码预算",
                value=current.get("wake_decode_budget_per_sec", 16.0),
                hint="token bucket 速率上限（越低 CPU 越省）",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_auto_name_variants",
                label="自动扩展名字变体",
                value=current.get("wake_auto_name_variants", True),
                kind="bool",
                default_bool=True,
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_auto_prefix_variants",
                label="自动扩展前缀变体",
                value=current.get("wake_auto_prefix_variants", True),
                kind="bool",
                default_bool=True,
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_allow_name_only",
                label="允许名字单独唤醒",
                value=current.get("wake_allow_name_only", True),
                kind="bool",
                default_bool=True,
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_speech_confirm_s",
                label="开口确认时长 (s)",
                value=current.get("wake_speech_confirm_s", 0.18),
                hint="累计语音证据达到该时长，判定已开口",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_use_semantic_gate",
                label="启用语义门控",
                value=current.get("wake_use_semantic_gate", False),
                kind="bool",
                default_bool=False,
                hint="通过轻量语义探测辅助判断开始/结束",
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_semantic_probe_interval_s",
                label="语义探测间隔 (s)",
                value=current.get("wake_semantic_probe_interval_s", 0.45),
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_semantic_window_s",
                label="语义探测窗口 (s)",
                value=current.get("wake_semantic_window_s", 1.2),
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_semantic_end_silence_s",
                label="语义静音结束 (s)",
                value=current.get("wake_semantic_end_silence_s", 1.0),
            )
            row = _add_field(
                sec_wake_advanced,
                row,
                key="wake_semantic_min_chars",
                label="语义最小字符数",
                value=current.get("wake_semantic_min_chars", 1),
            )
            _add_field(
                sec_wake_advanced,
                row,
                key="wake_semantic_timeout_ms",
                label="语义探测超时 (ms)",
                value=current.get("wake_semantic_timeout_ms", 1200),
            )

            status_label = Gtk.Label(label="已载入当前配置。保存后会按设置类型立即生效、下次录音生效，或在必要时重启后端。")
            status_label.set_xalign(0.0)
            status_label.set_opacity(0.78)
            status_label_ref["widget"] = status_label

            def _set_status(text: str) -> None:
                status_label.set_text(text)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            footer.pack_start(status_label, True, True, 0)
            root_box.pack_start(footer, False, False, 0)

            def _load_selected_preset() -> None:
                selected = preset_combo.get_active_text()
                if not selected:
                    preset_buffer.set_text("")
                    preset_text.set_sensitive(False)
                    btn_save_preset.set_sensitive(False)
                    btn_delete_preset.set_sensitive(False)
                    btn_set_current.set_sensitive(False)
                    return
                try:
                    content = preset_manager.get_preset_path(selected).read_text(encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    _set_status(f"读取预设失败：{exc}")
                    content = ""
                preset_buffer.set_text(content)
                preset_text.set_sensitive(True)
                btn_save_preset.set_sensitive(True)
                btn_delete_preset.set_sensitive(True)
                btn_set_current.set_sensitive(True)

            def _reload_preset_combo(prefer: str | None = None) -> None:
                names = _list_editable_refine_presets()
                preset_combo.remove_all()
                for name in names:
                    preset_combo.append_text(name)

                if not names:
                    preset_combo.set_active(-1)
                    _load_selected_preset()
                    return

                target = prefer or _get_current_refine_preset_name()
                if target in names:
                    idx = names.index(target)
                else:
                    idx = 0
                preset_combo.set_active(idx)
                _load_selected_preset()

            def _create_preset(*_args: object) -> None:
                name = preset_name_entry.get_text().strip()
                if not name:
                    _set_status("请输入预设名称")
                    return
                allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
                if not all(ch in allowed_chars for ch in name):
                    _set_status("预设名仅允许英文/数字/_/-")
                    return
                lower_name = name.lower()
                if lower_name == "readme" or lower_name.startswith("asr-"):
                    _set_status("该名称不可用，请更换")
                    return

                path = preset_manager.get_preset_path(name)
                if path.exists():
                    _set_status(f"预设已存在：{name}")
                    return

                template = f"# {name}\n\n请整理以下文本，保持原意并修正口语化表达。\n\n原文：{{text}}\n"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(template, encoding="utf-8")
                    preset_manager.clear_cache()
                    _reload_preset_combo(prefer=name)
                    preset_name_entry.set_text("")
                    _set_status(f"已新建预设：{name}；如需使用，请点击“设为当前”。")
                    self._update_tray_menu()
                except Exception as exc:  # noqa: BLE001
                    _set_status(f"新建失败：{exc}")

            def _save_selected_preset(*_args: object) -> None:
                selected = preset_combo.get_active_text()
                if not selected:
                    _set_status("请先选择预设")
                    return
                start_iter, end_iter = preset_buffer.get_bounds()
                content = preset_buffer.get_text(start_iter, end_iter, True).strip()
                if not content:
                    _set_status("预设内容不能为空")
                    return
                if "{text}" not in content:
                    _set_status("预设内容需包含 {text} 占位符")
                    return

                try:
                    preset_manager.get_preset_path(selected).write_text(content + "\n", encoding="utf-8")
                    preset_manager.clear_cache()
                    _set_status(f"预设已保存：{selected}")
                except Exception as exc:  # noqa: BLE001
                    _set_status(f"保存失败：{exc}")

            def _delete_selected_preset(*_args: object) -> None:
                selected = preset_combo.get_active_text()
                if not selected:
                    _set_status("请先选择预设")
                    return

                dialog = Gtk.MessageDialog(
                    transient_for=win,
                    flags=0,
                    message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.OK_CANCEL,
                    text=f"确认删除预设：{selected}？",
                )
                dialog.format_secondary_text("删除后无法恢复。")
                response = dialog.run()
                dialog.destroy()
                if response != Gtk.ResponseType.OK:
                    return

                try:
                    preset_manager.get_preset_path(selected).unlink(missing_ok=False)
                    preset_manager.clear_cache()
                    names = _list_editable_refine_presets()
                    fallback = "default" if "default" in names else (names[0] if names else "")
                    _reload_preset_combo(prefer=fallback)
                    if _get_current_refine_preset_name() == selected:
                        if fallback:
                            _set_current_refine_preset_name(fallback)
                            self.switch_preset(fallback)
                            _set_status(f"已删除预设：{selected}；当前预设已切换为：{fallback}")
                        else:
                            _set_status(f"已删除预设：{selected}；当前没有可用预设。")
                    else:
                        _set_status(f"已删除预设：{selected}")
                    self._update_tray_menu()
                except FileNotFoundError:
                    _set_status("预设文件不存在")
                except Exception as exc:  # noqa: BLE001
                    _set_status(f"删除失败：{exc}")

            def _set_selected_as_current(*_args: object) -> None:
                selected = preset_combo.get_active_text()
                if not selected:
                    _set_status("请先选择预设")
                    return
                try:
                    # 与托盘菜单行为保持一致：立即写配置并按设置语义生效
                    self.switch_preset(str(selected))
                    _set_current_refine_preset_name(str(selected))
                    _set_status(f"当前精炼预设已设为：{selected}（{effect_label(combined_setting_effect(['refine_preset']))}）")
                except Exception as exc:  # noqa: BLE001
                    _set_status(f"设置失败：{exc}")

            preset_combo.connect("changed", lambda *_args: _load_selected_preset())
            btn_create.connect("clicked", _create_preset)
            btn_save_preset.connect("clicked", _save_selected_preset)
            btn_delete_preset.connect("clicked", _delete_selected_preset)
            btn_refresh_preset.connect("clicked", lambda *_args: _reload_preset_combo())
            btn_set_current.connect("clicked", _set_selected_as_current)
            preset_name_entry.connect("activate", _create_preset)
            _reload_preset_combo()

            def _get_value(key: str) -> object:
                kind, widget = entries[key]
                if kind == "bool":
                    return bool(widget.get_active())
                if kind == "combo":
                    text = widget.get_active_text()
                    return text if text is not None else ""
                return widget.get_text()

            def _set_entry_text(key: str, value: str) -> None:
                target = entries.get(key)
                if not target:
                    return
                kind, widget = target
                if kind == "entry" and hasattr(widget, "set_text"):
                    widget.set_text(value)

            def _set_bool_switch(key: str, value: bool) -> None:
                target = entries.get(key)
                if not target:
                    return
                kind, widget = target
                if kind == "bool" and hasattr(widget, "set_active"):
                    widget.set_active(bool(value))

            btn_record_owner_sample.connect("clicked", lambda *_: self.open_speaker_enrollment_wizard())

            def _save(*, restart_backend: bool) -> None:
                latest_config: dict[str, object] = {}
                changed_keys: list[str] = []

                def _parse_int_field(key: str, default: int) -> int:
                    raw = str(_get_value(key)).strip()
                    return int(raw) if raw else default

                def _parse_float_field(key: str, default: float) -> float:
                    raw = str(_get_value(key)).strip()
                    return float(raw) if raw else default

                def _parse_csv_field(key: str, default: list[str]) -> list[str]:
                    raw = str(_get_value(key)).strip()
                    if not raw:
                        return list(default)
                    return [item.strip() for item in raw.split(",") if item.strip()]

                try:
                    latest_config = ConfigManager.load(self.config_path)
                    payload = {
                        "hotkey": str(_get_value("hotkey")).strip(),
                        "stop_hotkey": str(_get_value("stop_hotkey")).strip(),
                        "toggle_hotkey": str(_get_value("toggle_hotkey")).strip(),
                        "exit_hotkey": str(latest_config.get("exit_hotkey", "<ctrl>+<alt>+q")).strip(),
                        "cooldown_ms": _parse_int_field("cooldown_ms", int(current.get("cooldown_ms", 300))),
                        "trigger_mode": str(_get_value("trigger_mode")).strip() or str(current.get("trigger_mode", "ptt")),
                        "notify_backend": str(_get_value("notify_backend")).strip() or str(current.get("notify_backend", "auto")),
                        "duration": _parse_float_field("duration", float(current.get("duration", 4.0))),
                        "sample_rate": _parse_int_field("sample_rate", int(current.get("sample_rate", 16000))),
                        "channels": _parse_int_field("channels", int(current.get("channels", 1))),
                        "input_device": str(_get_value("input_device")).strip() or str(current.get("input_device", "default")),
                        "record_format": str(_get_value("record_format")).strip(),
                        "record_backend": str(_get_value("record_backend")).strip(),
                        "commit_backend": str(_get_value("commit_backend")).strip(),
                        "auto_hard_enter": bool(_get_value("auto_hard_enter")),
                        "asr_provider": str(_get_value("asr_provider")).strip() or str(current.get("asr_provider", "qwen-asr")),
                        "qwen_model": str(_get_value("qwen_model")).strip(),
                        "qwen_language": str(_get_value("qwen_language")).strip() or str(current.get("qwen_language", "Chinese")),
                        "qwen_max_new_tokens": _parse_int_field("qwen_max_new_tokens", int(current.get("qwen_max_new_tokens", 1024))),
                        "asr_context_preset": str(_get_value("asr_context_preset")).strip(),
                        "asr_context": str(_get_value("asr_context")).strip(),
                        "asr_endpoint": str(_get_value("asr_endpoint")).strip() or str(
                            current.get("asr_endpoint", "http://127.0.0.1:8000/v1/audio/transcriptions")
                        ),
                        "asr_api_key": str(_get_value("asr_api_key")).strip(),
                        "asr_timeout_s": _parse_float_field("asr_timeout_s", float(current.get("asr_timeout_s", 30.0))),
                        "device": str(_get_value("device")).strip() or str(current.get("device", "cuda")),
                        "enable_text_refine": bool(_get_value("enable_text_refine")),
                        "refine_provider": str(_get_value("refine_provider")).strip(),
                        "refine_preset": _get_current_refine_preset_name(),
                        "refine_model": str(_get_value("refine_model")).strip(),
                        "refine_device": str(_get_value("refine_device")).strip() or str(current.get("refine_device", "cuda")),
                        "refine_n_gpu_layers": _parse_int_field("refine_n_gpu_layers", int(current.get("refine_n_gpu_layers", -1))),
                        "refine_max_tokens": _parse_int_field("refine_max_tokens", int(current.get("refine_max_tokens", 512))),
                        "enable_thinking": bool(_get_value("enable_thinking")),
                        "refine_api_base": str(_get_value("refine_api_base")).strip(),
                        "refine_api_key": str(_get_value("refine_api_key")).strip(),
                        "refine_api_model": str(_get_value("refine_api_model")).strip(),
                        "warmup": bool(_get_value("warmup")),
                        "debug_diagnostics": bool(_get_value("debug_diagnostics")),
                        "enable_voice_wake": bool(_get_value("enable_voice_wake")),
                        "wake_prefix": _parse_csv_field("wake_prefix", list(current.get("wake_prefix", ["嗨", "嘿"]))),
                        "wake_name": _parse_csv_field("wake_name", list(current.get("wake_name", ["小二"]))),
                        "wake_cooldown_s": _parse_float_field("wake_cooldown_s", float(current.get("wake_cooldown_s", 3.0))),
                        "wake_auto_stop_silence_s": _parse_float_field(
                            "wake_auto_stop_silence_s",
                            float(current.get("wake_auto_stop_silence_s", 1.5)),
                        ),
                        "wake_min_speech_s": _parse_float_field("wake_min_speech_s", float(current.get("wake_min_speech_s", 0.5))),
                        "wake_use_webrtcvad": bool(_get_value("wake_use_webrtcvad")),
                        "wake_vad_aggressiveness": _parse_int_field("wake_vad_aggressiveness", int(current.get("wake_vad_aggressiveness", 2))),
                        "wake_vad_frame_ms": _parse_int_field("wake_vad_frame_ms", int(current.get("wake_vad_frame_ms", 30))),
                        "wake_no_speech_timeout_s": _parse_float_field(
                            "wake_no_speech_timeout_s",
                            float(current.get("wake_no_speech_timeout_s", 2.0)),
                        ),
                        "wake_speech_confirm_s": _parse_float_field(
                            "wake_speech_confirm_s",
                            float(current.get("wake_speech_confirm_s", 0.18)),
                        ),
                        "wake_stats": bool(_get_value("wake_stats")),
                        "wake_pre_vad": bool(_get_value("wake_pre_vad")),
                        "wake_pre_vad_aggressiveness": _parse_int_field(
                            "wake_pre_vad_aggressiveness",
                            int(current.get("wake_pre_vad_aggressiveness", 3)),
                        ),
                        "wake_pre_vad_frame_ms": _parse_int_field(
                            "wake_pre_vad_frame_ms",
                            int(current.get("wake_pre_vad_frame_ms", 30)),
                        ),
                        "wake_pre_vad_enter_frames": _parse_int_field(
                            "wake_pre_vad_enter_frames",
                            int(current.get("wake_pre_vad_enter_frames", 4)),
                        ),
                        "wake_pre_vad_hangover_ms": _parse_int_field(
                            "wake_pre_vad_hangover_ms",
                            int(current.get("wake_pre_vad_hangover_ms", 120)),
                        ),
                        "wake_pre_roll_ms": _parse_int_field("wake_pre_roll_ms", int(current.get("wake_pre_roll_ms", 300))),
                        "wake_decode_budget_per_cycle": _parse_int_field(
                            "wake_decode_budget_per_cycle",
                            int(current.get("wake_decode_budget_per_cycle", 1)),
                        ),
                        "wake_decode_budget_per_sec": _parse_float_field(
                            "wake_decode_budget_per_sec",
                            float(current.get("wake_decode_budget_per_sec", 16.0)),
                        ),
                        "wake_auto_name_variants": bool(_get_value("wake_auto_name_variants")),
                        "wake_auto_prefix_variants": bool(_get_value("wake_auto_prefix_variants")),
                        "wake_allow_name_only": bool(_get_value("wake_allow_name_only")),
                        "wake_use_semantic_gate": bool(_get_value("wake_use_semantic_gate")),
                        "wake_semantic_probe_interval_s": _parse_float_field(
                            "wake_semantic_probe_interval_s",
                            float(current.get("wake_semantic_probe_interval_s", 0.45)),
                        ),
                        "wake_semantic_window_s": _parse_float_field(
                            "wake_semantic_window_s",
                            float(current.get("wake_semantic_window_s", 1.2)),
                        ),
                        "wake_semantic_end_silence_s": _parse_float_field(
                            "wake_semantic_end_silence_s",
                            float(current.get("wake_semantic_end_silence_s", 1.0)),
                        ),
                        "wake_semantic_min_chars": _parse_int_field(
                            "wake_semantic_min_chars",
                            int(current.get("wake_semantic_min_chars", 1)),
                        ),
                        "wake_semantic_timeout_ms": _parse_int_field(
                            "wake_semantic_timeout_ms",
                            int(current.get("wake_semantic_timeout_ms", 1200)),
                        ),
                        "wake_owner_verify": bool(_get_value("wake_owner_verify")),
                        "wake_owner_sample": str(_get_value("wake_owner_sample")).strip(),
                        "wake_owner_profile": str(_get_value("wake_owner_profile")).strip()
                        or str(current.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json")),
                        "wake_owner_threshold": _parse_float_field(
                            "wake_owner_threshold",
                            float(current.get("wake_owner_threshold", 0.72)),
                        ),
                        "wake_owner_window_s": _parse_float_field(
                            "wake_owner_window_s",
                            float(current.get("wake_owner_window_s", 1.6)),
                        ),
                        "wake_owner_silence_extend_s": _parse_float_field(
                            "wake_owner_silence_extend_s",
                            float(current.get("wake_owner_silence_extend_s", 0.5)),
                        ),
                        "sound_on_path": str(_get_value("sound_on_path")).strip(),
                        "sound_off_path": str(_get_value("sound_off_path")).strip(),
                        # Legacy key kept for backward compatibility; when present it acts as fallback.
                        "wake_beep_path": str(latest_config.get("wake_beep_path", "")).strip(),
                        "wake_encoder": str(_get_value("wake_encoder")).strip(),
                        "wake_decoder": str(_get_value("wake_decoder")).strip(),
                        "wake_joiner": str(_get_value("wake_joiner")).strip(),
                        "wake_tokens": str(_get_value("wake_tokens")).strip(),
                        "wake_keywords_file": str(_get_value("wake_keywords_file")).strip(),
                        "wake_tokens_type": str(_get_value("wake_tokens_type")).strip() or str(current.get("wake_tokens_type", "ppinyin")),
                        "wake_provider": str(_get_value("wake_provider")).strip() or str(current.get("wake_provider", "cpu")),
                        "wake_num_threads": _parse_int_field(
                            "wake_num_threads",
                            int(current.get("wake_num_threads", DEFAULT_WAKE_NUM_THREADS)),
                        ),
                        "wake_sample_rate": _parse_int_field("wake_sample_rate", int(current.get("wake_sample_rate", 16000))),
                        "wake_keyword_score": _parse_float_field(
                            "wake_keyword_score",
                            float(current.get("wake_keyword_score", 1.5)),
                        ),
                        "wake_keyword_threshold": _parse_float_field(
                            "wake_keyword_threshold",
                            float(current.get("wake_keyword_threshold", DEFAULT_WAKE_KEYWORD_THRESHOLD)),
                        ),
                        "hub": latest_config.get("hub", "ms"),
                        "remote_code": latest_config.get("remote_code", ""),
                        "hotword": latest_config.get("hotword", []),
                        "enable_streaming_refine": latest_config.get("enable_streaming_refine", False),
                    }
                    payload = normalize_runtime_config(
                        payload,
                        include_sound_defaults=False,
                        allow_auto_fallback_commit=False,
                    )
                    if payload["wake_vad_aggressiveness"] not in {0, 1, 2, 3}:
                        payload["wake_vad_aggressiveness"] = 2
                    if payload["wake_vad_frame_ms"] not in {10, 20, 30}:
                        payload["wake_vad_frame_ms"] = 30
                    if payload["wake_pre_vad_aggressiveness"] not in {0, 1, 2, 3}:
                        payload["wake_pre_vad_aggressiveness"] = 3
                    if payload["wake_pre_vad_frame_ms"] not in {10, 20, 30}:
                        payload["wake_pre_vad_frame_ms"] = 30
                    payload["wake_no_speech_timeout_s"] = max(0.0, float(payload["wake_no_speech_timeout_s"]))
                    payload["wake_speech_confirm_s"] = max(0.0, float(payload["wake_speech_confirm_s"]))
                    payload["wake_pre_vad_enter_frames"] = max(1, int(payload["wake_pre_vad_enter_frames"]))
                    payload["wake_pre_vad_hangover_ms"] = max(0, int(payload["wake_pre_vad_hangover_ms"]))
                    payload["wake_pre_roll_ms"] = max(0, int(payload["wake_pre_roll_ms"]))
                    payload["wake_decode_budget_per_cycle"] = max(1, int(payload["wake_decode_budget_per_cycle"]))
                    payload["wake_decode_budget_per_sec"] = max(1.0, float(payload["wake_decode_budget_per_sec"]))
                    payload["wake_semantic_probe_interval_s"] = max(0.1, float(payload["wake_semantic_probe_interval_s"]))
                    payload["wake_semantic_window_s"] = max(0.4, float(payload["wake_semantic_window_s"]))
                    payload["wake_semantic_end_silence_s"] = max(0.2, float(payload["wake_semantic_end_silence_s"]))
                    payload["wake_semantic_min_chars"] = max(1, int(payload["wake_semantic_min_chars"]))
                    payload["wake_semantic_timeout_ms"] = max(200, int(payload["wake_semantic_timeout_ms"]))
                    payload["wake_owner_threshold"] = min(0.99, max(0.0, float(payload["wake_owner_threshold"])))
                    payload["wake_owner_window_s"] = max(0.6, float(payload["wake_owner_window_s"]))
                    payload["wake_owner_silence_extend_s"] = max(0.0, float(payload["wake_owner_silence_extend_s"]))
                    effect, restarted, changed_keys = _save_config_changes(
                        self.config_path,
                        payload,
                        apply_now=restart_backend,
                        restart_callback=lambda: self.root.after(0, self.backend.restart),
                    )
                except ValueError as exc:
                    status_label.set_text(f"保存失败：数值格式不正确 ({exc})")
                    return
                except Exception as exc:  # noqa: BLE001
                    status_label.set_text(f"保存失败：{exc}")
                    return

                status_label.set_text(f"{effect_status_message(effect, restarted=restarted)} ({self.config_path})")
                self._update_tray_menu()

            btn_save = Gtk.Button(label="仅保存")
            btn_save.connect("clicked", lambda *_: _save(restart_backend=False))
            footer.pack_end(btn_save, False, False, 0)

            btn_save_restart = Gtk.Button(label="保存并应用")
            btn_save_restart.connect("clicked", lambda *_: _save(restart_backend=True))
            footer.pack_end(btn_save_restart, False, False, 0)

            btn_close = Gtk.Button(label="关闭")
            btn_close.connect("clicked", lambda *_: win.destroy())
            footer.pack_end(btn_close, False, False, 0)

            def _on_destroy(*_args: object) -> None:
                self._gtk_settings_window = None

            win.connect("destroy", _on_destroy)
            win.show_all()
            win.present()
            return False

        self._glib.idle_add(_on_gtk_thread)

    def _start_tray(self) -> None:
        # Use AppIndicator3 (GNOME native) - Ubuntu only
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

        # Use PNG icon directly
        logo_path = get_logo_path("idle")
        icon_path = str(logo_path.absolute())
        self._appindicator_png_cache: dict[str, str] = {}

        if logo_path.exists():
            self._appindicator_png_cache["idle"] = icon_path
            print(f"AppIndicator3 icon (PNG): {icon_path}", file=sys.stderr)
        else:
            print(f"Logo not found at {icon_path}, using system icon", file=sys.stderr)
            icon_path = "audio-input-microphone"

        # Create indicator
        self.indicator = AppIndicator3.Indicator.new(
            "recordian",
            icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Recordian")

        # Create menu
        menu = Gtk.Menu()

        # 状态栏：仅显示时间
        if self.state.last_total_ms > 0:
            time_label = f"时间: {self.state.last_total_ms:.0f} ms"
        else:
            time_label = "时间: --"
        status_item = Gtk.MenuItem(label=time_label)
        status_item.set_sensitive(False)
        menu.append(status_item)
        self._appindicator_status_item = status_item

        menu.append(Gtk.SeparatorMenuItem())

        # 启动后端
        start_item = Gtk.MenuItem(label="启动后端")
        start_item.connect("activate", lambda _: self.root.after(0, self.backend.start))
        menu.append(start_item)

        # 停止后端
        stop_item = Gtk.MenuItem(label="停止后端")
        stop_item.connect("activate", lambda _: self.root.after(0, self.backend.stop))
        menu.append(stop_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Text refine toggle
        text_refine_item = Gtk.CheckMenuItem(label="启用文本精炼")
        config = ConfigManager.load(self.config_path)
        text_refine_enabled = bool(config.get("enable_text_refine", True))
        text_refine_item.set_active(text_refine_enabled)
        text_refine_item.connect("toggled", lambda item: self.root.after(0, lambda: self.toggle_text_refine(item.get_active())))
        menu.append(text_refine_item)
        self._appindicator_text_refine_item = text_refine_item

        voice_wake_item = Gtk.CheckMenuItem(label="语音唤醒模式")
        voice_wake_enabled = bool(config.get("enable_voice_wake", False))
        voice_wake_item.set_active(voice_wake_enabled)
        voice_wake_item.connect("toggled", lambda item: self.root.after(0, lambda: self.toggle_voice_wake(item.get_active())))
        menu.append(voice_wake_item)
        self._appindicator_voice_wake_item = voice_wake_item

        auto_hard_enter_item = Gtk.CheckMenuItem(label="自动硬回车")
        auto_hard_enter_enabled = bool(config.get("auto_hard_enter", False))
        auto_hard_enter_item.set_active(auto_hard_enter_enabled)
        auto_hard_enter_item.connect("toggled", lambda item: self.root.after(0, lambda: self.toggle_auto_hard_enter(item.get_active())))
        menu.append(auto_hard_enter_item)
        self._appindicator_auto_hard_enter_item = auto_hard_enter_item

        # Copy last text
        copy_text_item = Gtk.MenuItem(label="复制最后识别的文本")
        copy_text_item.connect("activate", lambda _: self.root.after(0, self.copy_last_text))
        copy_text_item.set_sensitive(bool(self.state.last_text))
        menu.append(copy_text_item)
        self._appindicator_copy_text_item = copy_text_item

        # 预设子菜单
        preset_menu_item = Gtk.MenuItem(label="切换预设")
        preset_submenu = Gtk.Menu()
        self._appindicator_preset_submenu = preset_submenu
        preset_menu_item.set_submenu(preset_submenu)
        menu.append(preset_menu_item)
        self._refresh_appindicator_preset_submenu()

        # 常用词管理
        context_item = Gtk.MenuItem(label="常用词管理...")
        context_item.connect("activate", lambda _: self.root.after(0, self.open_context_editor))
        menu.append(context_item)

        # 声纹注册向导
        speaker_enroll_item = Gtk.MenuItem(label="声纹注册向导...")
        speaker_enroll_item.connect("activate", lambda _: self.root.after(0, self.open_speaker_enrollment_wizard))
        menu.append(speaker_enroll_item)

        # 设置
        settings_item = Gtk.MenuItem(label="设置...")
        settings_item.connect("activate", lambda _: self.root.after(0, self.open_settings))
        menu.append(settings_item)

        menu.append(Gtk.SeparatorMenuItem())

        # 退出
        quit_item = Gtk.MenuItem(label="退出")
        quit_item.connect("activate", lambda _: self.root.after(0, self.quit))
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        self.icon = None  # Mark that we're using AppIndicator instead

        # Start Gtk main loop in a background thread — required for AppIndicator3 to work
        self._gtk_thread = threading.Thread(target=Gtk.main, daemon=True, name="gtk-main")
        self._gtk_thread.start()
        print("Gtk main loop started", file=sys.stderr)

    def _update_tray_menu(self) -> None:
        # Update AppIndicator status
        if hasattr(self, 'indicator') and self.indicator is not None:
            status = self.state.status
            cache = getattr(self, '_appindicator_png_cache', {})
            if status not in cache:
                logo_path = get_logo_path(status)
                icon_path = str(logo_path.absolute())
                if logo_path.exists():
                    cache[status] = icon_path
                else:
                    # Fallback to idle logo
                    cache[status] = cache.get("idle", icon_path)
            icon_path = cache[status]

            # Gtk operations must run on the Gtk thread — use GLib.idle_add
            if hasattr(self, '_glib'):
                indicator = self.indicator
                status_item = getattr(self, '_appindicator_status_item', None)
                if self.state.last_total_ms > 0:
                    label = f"时间: {self.state.last_total_ms:.0f} ms"
                else:
                    label = "时间: --"

                def _gtk_update():
                    if status_item is not None:
                        status_item.set_label(label)
                    # Update copy text item sensitivity
                    copy_text_item = getattr(self, '_appindicator_copy_text_item', None)
                    if copy_text_item is not None:
                        copy_text_item.set_sensitive(bool(self.state.last_text))
                    cfg = ConfigManager.load(self.config_path)
                    text_refine_item = getattr(self, "_appindicator_text_refine_item", None)
                    if text_refine_item is not None:
                        text_refine_item.set_active(bool(cfg.get("enable_text_refine", True)))
                    voice_wake_item = getattr(self, "_appindicator_voice_wake_item", None)
                    if voice_wake_item is not None:
                        voice_wake_item.set_active(bool(cfg.get("enable_voice_wake", False)))
                    auto_hard_enter_item = getattr(self, "_appindicator_auto_hard_enter_item", None)
                    if auto_hard_enter_item is not None:
                        auto_hard_enter_item.set_active(bool(cfg.get("auto_hard_enter", False)))
                    self._sync_appindicator_preset_submenu()
                    try:
                        indicator.set_icon(icon_path)
                    except Exception:
                        pass

                self._glib.idle_add(_gtk_update)

    def quit(self) -> None:
        self.backend.stop()
        self.overlay.shutdown()

        # Stop GTK settings window and AppIndicator on GTK thread
        if hasattr(self, '_glib'):
            def _gtk_cleanup():
                if self._gtk_settings_window is not None:
                    try:
                        self._gtk_settings_window.destroy()
                    except Exception:
                        pass
                    self._gtk_settings_window = None

                # Stop AppIndicator
                if hasattr(self, 'indicator') and self.indicator is not None:
                    try:
                        import gi
                        gi.require_version('AppIndicator3', '0.1')
                        from gi.repository import AppIndicator3
                        self.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
                    except Exception:
                        pass
                    # Stop Gtk main loop
                    if hasattr(self, '_gtk'):
                        try:
                            self._gtk.main_quit()
                        except Exception:
                            pass

            self._glib.idle_add(_gtk_cleanup)
        else:
            # Fallback if GTK not initialized
            if self._gtk_settings_window is not None:
                self._gtk_settings_window = None

        self.root.quit()
        self.root.destroy()


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


HOTKEY_CAPTURE_FIELDS = {"hotkey", "stop_hotkey", "toggle_hotkey"}
_HOTKEY_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd", "menu")


def _normalize_hotkey_token(raw: str) -> str:
    token = raw.strip().lower()
    alias = {
        "control_l": "ctrl_l",
        "control_r": "ctrl_r",
        "control": "ctrl",
        "alt_l": "alt_l",
        "alt_r": "alt_r",
        "iso_level3_shift": "alt_gr",
        "shift_l": "shift_l",
        "shift_r": "shift_r",
        "super_l": "cmd_l",
        "super_r": "cmd_r",
        "meta_l": "cmd_l",
        "meta_r": "cmd_r",
        "win_l": "cmd_l",
        "win_r": "cmd_r",
        "return": "enter",
        "kp_enter": "enter",
        "escape": "esc",
        "esc": "esc",
        "space": "space",
        "spacebar": "space",
        "prior": "page_up",
        "next": "page_down",
        "print": "print_screen",
    }
    token = alias.get(token, token)
    if token.startswith("kp_") and len(token) > 3:
        keypad_token = token[3:]
        keypad_alias = {
            "add": "+",
            "subtract": "-",
            "multiply": "*",
            "divide": "/",
            "decimal": ".",
            "separator": ",",
        }
        token = keypad_alias.get(keypad_token, keypad_token)
    return token


def _format_hotkey_spec(*, modifiers: set[str], key: str) -> str:
    if key in {"ctrl_l", "ctrl_r"}:
        modifiers.discard("ctrl")
    elif key in {"alt_l", "alt_r", "alt_gr"}:
        modifiers.discard("alt")
    elif key in {"shift_l", "shift_r"}:
        modifiers.discard("shift")
    elif key in {"cmd_l", "cmd_r"}:
        modifiers.discard("cmd")

    parts: list[str] = [mod for mod in _HOTKEY_MODIFIER_ORDER if mod in modifiers]
    if key and key not in parts:
        parts.append(key)
    if not parts:
        return ""
    return "+".join(f"<{part}>" for part in parts)


def _build_gtk_hotkey_spec(event: object, gdk: Any) -> str:
    keyval = getattr(event, "keyval", None)
    if keyval is None:
        return ""
    key_name = gdk.keyval_name(keyval)
    if not key_name:
        return ""
    key = _normalize_hotkey_token(key_name)
    if not key:
        return ""

    state = getattr(event, "state", 0)
    modifiers: set[str] = set()
    if state & gdk.ModifierType.CONTROL_MASK:
        modifiers.add("ctrl")
    if state & gdk.ModifierType.SHIFT_MASK:
        modifiers.add("shift")
    if state & gdk.ModifierType.MOD1_MASK:
        modifiers.add("alt")
    if hasattr(gdk.ModifierType, "SUPER_MASK") and state & gdk.ModifierType.SUPER_MASK:
        modifiers.add("cmd")
    if hasattr(gdk.ModifierType, "META_MASK") and state & gdk.ModifierType.META_MASK:
        modifiers.add("cmd")
    return _format_hotkey_spec(modifiers=modifiers, key=key)


def _parse_bool(value: str, *, default: bool) -> bool:
    token = value.strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _hex_with_alpha(color: str, alpha: float) -> str:
    # tkinter does not support #RRGGBBAA, so we blend against the dark bg.
    return _blend_hex("#0b0f1a", color, alpha)


def _blend_hex(a: str, b: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    a = a.lstrip("#")
    b = b.lstrip("#")
    if len(a) != 6 or len(b) != 6:
        return "#ffffff"
    ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
    br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    rr = int(ar * (1.0 - ratio) + br * ratio)
    rg = int(ag * (1.0 - ratio) + bg * ratio)
    rb = int(ab * (1.0 - ratio) + bb * ratio)
    return f"#{rr:02x}{rg:02x}{rb:02x}"


def main() -> None:
    import sys

    from recordian.error_tracker import get_error_tracker

    def handle_exception(exc_type, exc_value, exc_traceback):
        """Global exception handler."""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(exc_value)

    sys.excepthook = handle_exception

    try:
        args = build_parser().parse_args()
        app = TrayApp(args)
        app.run()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(e)
        raise


if __name__ == "__main__":
    main()
