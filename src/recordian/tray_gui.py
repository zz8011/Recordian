from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any

from recordian.config import ConfigManager
from recordian.backend_manager import BackendManager, parse_backend_event_line
from recordian.waveform_renderer import WaveformRenderer


DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recordian tray GUI with waveform overlay.")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--notify-backend", choices=["none", "auto", "notify-send", "stdout"], default="auto")
    return parser


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
        self._settings_window: tk.Toplevel | None = None

    def _on_backend_state_change(self, running: bool, status: str, detail: str) -> None:
        self.state.backend_running = running
        self.state.status = status
        self.state.detail = detail

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
            self.state.status = "recording"
            self.state.detail = "Recording..."
            self.overlay.set_state("recording", "Listening...")
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
            self.overlay.set_state("processing", "Recognizing...")
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
                self.overlay.set_state("idle", _truncate(text, 48))
            else:
                self.state.detail = "识别为空"
                self.overlay.set_state("idle", "No speech detected")
        elif et == "busy":
            self.state.status = "busy"
            self.state.detail = "Busy"
            self.overlay.set_state("processing", "Still processing previous input")
        elif et == "error":
            self.state.status = "error"
            self.state.detail = str(event.get("error", "error"))
            self.overlay.set_state("error", _truncate(self.state.detail, 72))
        elif et in {"stopped", "backend_exited"}:
            self.state.backend_running = False
            self.state.status = "stopped"
            self.state.detail = "Stopped"
            self.overlay.set_state("idle", "Stopped")
        elif et == "log":
            msg = str(event.get("message", "")).strip()
            if msg:
                self.state.detail = _truncate(msg, 48)
                if msg.startswith("diag "):
                    print(msg, file=sys.stderr, flush=True)
        self._update_tray_menu()

    def toggle_quick_mode(self, enabled: bool) -> None:
        """切换快速模式（跳过文字优化）- 热切换"""
        config = ConfigManager.load(self.config_path)
        config["enable_text_refine"] = not enabled  # enabled=True 表示快速模式，即不启用文字优化
        ConfigManager.save(self.config_path, config)

        # 热切换：只更新配置文件，不重启后端
        mode_text = "快速模式" if enabled else "质量模式"
        self.events.put({"event": "log", "message": f"已切换到{mode_text}（热切换）"})

        # 更新托盘菜单以反映新状态
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
        """切换文字优化 preset（热切换，不重启后端）"""
        config = ConfigManager.load(self.config_path)
        config["refine_preset"] = preset_name
        ConfigManager.save(self.config_path, config)

        # 热切换：只更新配置文件，后端下次录音时会读取新配置
        # 不需要重启后端，避免重新加载模型
        self.events.put({"event": "log", "message": f"已切换到 {preset_name} preset（热切换）"})

        # 更新托盘菜单以反映新的选中状态
        self._update_tray_menu()


    def open_settings(self) -> None:
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.lift()
            return

        current = ConfigManager.load(self.config_path)
        current_record_backend = str(current.get("record_backend", "auto"))
        if current_record_backend == "ffmpeg":
            current_record_backend = "ffmpeg-pulse"
        if current_record_backend not in {"auto", "ffmpeg-pulse", "arecord"}:
            current_record_backend = "auto"

        current_record_format = str(current.get("record_format", "ogg")).lower()
        if current_record_format == "mp3":
            current_record_format = "ogg"
        if current_record_format not in {"ogg", "wav"}:
            current_record_format = "ogg"

        current_refine_provider = str(current.get("refine_provider", "local"))
        if current_refine_provider == "llama.cpp":
            current_refine_provider = "llamacpp"
        if current_refine_provider not in {"local", "cloud", "llamacpp"}:
            current_refine_provider = "local"

        current_commit_backend = str(current.get("commit_backend", "auto"))
        if current_commit_backend == "pynput":
            current_commit_backend = "auto"
        if current_commit_backend not in {"none", "auto", "wtype", "xdotool", "xdotool-clipboard", "stdout"}:
            current_commit_backend = "auto"

        current_enable_thinking = current.get("enable_thinking", current.get("refine_enable_thinking", False))
        win = tk.Toplevel(self.root)
        win.title("Recordian Settings")
        win.geometry("600x700")
        win.attributes("-topmost", True)
        self._settings_window = win

        # Create scrollable frame
        canvas = tk.Canvas(win)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        fields = [
            # 热键设置
            ("hotkey", "触发热键", current.get("hotkey", "<ctrl_r>")),
            ("stop_hotkey", "停止热键", current.get("stop_hotkey", "")),
            ("toggle_hotkey", "切换热键", current.get("toggle_hotkey", "")),
            ("exit_hotkey", "退出热键", current.get("exit_hotkey", "<ctrl>+<alt>+q")),
            ("trigger_mode", "触发模式 (ptt/toggle)", current.get("trigger_mode", "ptt")),
            ("cooldown_ms", "冷却时间 (ms)", str(current.get("cooldown_ms", 300))),

            # 录音设置
            ("duration", "录音时长 (s)", str(current.get("duration", 4.0))),
            ("record_backend", "录音后端 (auto/ffmpeg-pulse/arecord)", current_record_backend),
            ("record_format", "录音格式 (ogg/wav)", current_record_format),
            ("sample_rate", "采样率", str(current.get("sample_rate", 16000))),
            ("channels", "声道数", str(current.get("channels", 1))),
            ("input_device", "输入设备", current.get("input_device", "default")),

            # ASR 设置
            ("asr_provider", "ASR Provider (qwen-asr)", current.get("asr_provider", "qwen-asr")),
            ("qwen_model", "Qwen ASR 模型路径", current.get("qwen_model", "")),
            ("qwen_language", "Qwen 语言 (Chinese/auto)", current.get("qwen_language", "Chinese")),
            ("qwen_max_new_tokens", "Qwen Max Tokens", str(current.get("qwen_max_new_tokens", 1024))),
            ("asr_context_preset", "ASR Context 预设 (留空/default/formal/meeting/technical/simple)", current.get("asr_context_preset", "")),
            ("asr_context", "ASR Context 自定义 (优先级低于预设)", current.get("asr_context", "")),
            ("device", "计算设备 (cpu/cuda/auto)", current.get("device", "cuda")),

            # 文本精炼设置
            ("enable_text_refine", "启用文本精炼", current.get("enable_text_refine", False)),
            ("refine_provider", "精炼 Provider (local/cloud/llamacpp)", current_refine_provider),
            ("refine_preset", "精炼预设", current.get("refine_preset", "default")),
            ("refine_model", "本地精炼模型路径（local=HF路径，llamacpp=GGUF路径）", current.get("refine_model", "")),
            ("refine_device", "精炼设备 (cpu/cuda)", current.get("refine_device", "cuda")),
            ("refine_n_gpu_layers", "llama.cpp GPU 层数 (-1=全部)", str(current.get("refine_n_gpu_layers", -1))),
            ("refine_max_tokens", "精炼 Max Tokens", str(current.get("refine_max_tokens", 512))),
            ("enable_thinking", "启用 Thinking 模式", current_enable_thinking),
            ("refine_api_base", "云端 API Base", current.get("refine_api_base", "")),
            ("refine_api_key", "云端 API Key", current.get("refine_api_key", "")),
            ("refine_api_model", "云端 API 模型", current.get("refine_api_model", "")),

            # 上屏设置
            ("commit_backend", "上屏后端 (auto/wtype/xdotool/xdotool-clipboard)", current_commit_backend),

            # 其他设置
            ("warmup", "启动时预热", current.get("warmup", True)),
            ("debug_diagnostics", "调试诊断", current.get("debug_diagnostics", False)),
            ("notify_backend", "通知后端 (auto/notify-send/none)", current.get("notify_backend", "auto")),
        ]

        entries: dict[str, tk.StringVar] = {}
        frm = ttk.Frame(scrollable_frame, padding=12)
        frm.pack(fill="both", expand=True)

        for row, (key, label, value) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(value))
            entries[key] = var
            ttk.Entry(frm, textvariable=var, width=54).grid(row=row, column=1, sticky="we", pady=4)

        frm.columnconfigure(1, weight=1)
        status_var = tk.StringVar(value=f"配置文件: {self.config_path}")
        ttk.Label(frm, textvariable=status_var).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=10)

        def _save() -> None:
            record_format = entries["record_format"].get().strip().lower() or "ogg"
            if record_format == "mp3":
                record_format = "ogg"
            if record_format not in {"ogg", "wav"}:
                record_format = "ogg"

            record_backend = entries["record_backend"].get().strip() or "auto"
            if record_backend == "ffmpeg":
                record_backend = "ffmpeg-pulse"
            if record_backend not in {"auto", "ffmpeg-pulse", "arecord"}:
                record_backend = "auto"

            refine_provider = entries["refine_provider"].get().strip() or "local"
            if refine_provider == "llama.cpp":
                refine_provider = "llamacpp"
            if refine_provider not in {"local", "cloud", "llamacpp"}:
                refine_provider = "local"

            commit_backend = entries["commit_backend"].get().strip() or "auto"
            if commit_backend == "pynput":
                commit_backend = "auto"
            if commit_backend not in {"none", "auto", "wtype", "xdotool", "xdotool-clipboard", "stdout"}:
                commit_backend = "auto"

            payload = {
                "hotkey": entries["hotkey"].get().strip(),
                "stop_hotkey": entries["stop_hotkey"].get().strip(),
                "toggle_hotkey": entries["toggle_hotkey"].get().strip(),
                "exit_hotkey": entries["exit_hotkey"].get().strip(),
                "cooldown_ms": int(entries["cooldown_ms"].get().strip() or "300"),
                "trigger_mode": entries["trigger_mode"].get().strip() or "ptt",
                "notify_backend": entries["notify_backend"].get().strip() or "auto",
                "duration": float(entries["duration"].get().strip() or "4.0"),
                "sample_rate": int(entries["sample_rate"].get().strip() or "16000"),
                "channels": int(entries["channels"].get().strip() or "1"),
                "input_device": entries["input_device"].get().strip() or "default",
                "record_format": record_format,
                "record_backend": record_backend,
                "commit_backend": commit_backend,
                "asr_provider": entries["asr_provider"].get().strip() or "qwen-asr",
                "qwen_model": entries["qwen_model"].get().strip(),
                "qwen_language": entries["qwen_language"].get().strip() or "Chinese",
                "qwen_max_new_tokens": int(entries["qwen_max_new_tokens"].get().strip() or "1024"),
                "asr_context_preset": entries["asr_context_preset"].get().strip(),
                "asr_context": entries["asr_context"].get().strip(),
                "device": entries["device"].get().strip() or "cuda",
                "enable_text_refine": _parse_bool(entries["enable_text_refine"].get(), default=False),
                "refine_provider": refine_provider,
                "refine_preset": entries["refine_preset"].get().strip() or "default",
                "refine_model": entries["refine_model"].get().strip(),
                "refine_device": entries["refine_device"].get().strip() or "cuda",
                "refine_n_gpu_layers": int(entries["refine_n_gpu_layers"].get().strip() or "-1"),
                "refine_max_tokens": int(entries["refine_max_tokens"].get().strip() or "512"),
                "enable_thinking": _parse_bool(entries["enable_thinking"].get(), default=False),
                "refine_api_base": entries["refine_api_base"].get().strip(),
                "refine_api_key": entries["refine_api_key"].get().strip(),
                "refine_api_model": entries["refine_api_model"].get().strip(),
                "warmup": _parse_bool(entries["warmup"].get(), default=True),
                "debug_diagnostics": _parse_bool(entries["debug_diagnostics"].get(), default=False),
                "hub": current.get("hub", "ms"),
                "remote_code": current.get("remote_code", ""),
                "hotword": current.get("hotword", []),
            }
            ConfigManager.save(self.config_path, payload)
            status_var.set(f"已保存并重启后端 ({self.config_path})")
            self.backend.restart()

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=10)
        ttk.Button(btns, text="保存并重启", command=_save).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=6)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

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

        # Status item (disabled)
        status_item = Gtk.MenuItem(label=f"Status: {self.state.status}")
        status_item.set_sensitive(False)
        menu.append(status_item)
        self._appindicator_status_item = status_item

        # Last text item (disabled, shows last recognized text)
        last_text = self.state.last_text[:50] + "..." if len(self.state.last_text) > 50 else self.state.last_text
        last_text_label = f"Last: {last_text}" if last_text else "Last: (无)"
        last_text_item = Gtk.MenuItem(label=last_text_label)
        last_text_item.set_sensitive(False)
        menu.append(last_text_item)
        self._appindicator_last_text_item = last_text_item

        # Performance stats item (disabled, shows last operation timing)
        if self.state.last_total_ms > 0:
            perf_label = f"Perf: {self.state.last_total_ms:.0f}ms (录:{self.state.last_record_ms:.0f} 识:{self.state.last_transcribe_ms:.0f} 优:{self.state.last_refine_ms:.0f})"
        else:
            perf_label = "Perf: (无数据)"
        perf_item = Gtk.MenuItem(label=perf_label)
        perf_item.set_sensitive(False)
        menu.append(perf_item)
        self._appindicator_perf_item = perf_item

        menu.append(Gtk.SeparatorMenuItem())

        # Start Backend
        start_item = Gtk.MenuItem(label="Start Backend")
        start_item.connect("activate", lambda _: self.root.after(0, self.backend.start))
        menu.append(start_item)

        # Stop Backend
        stop_item = Gtk.MenuItem(label="Stop Backend")
        stop_item.connect("activate", lambda _: self.root.after(0, self.backend.stop))
        menu.append(stop_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Quick Mode toggle
        quick_mode_item = Gtk.CheckMenuItem(label="快速模式（跳过文字优化）")
        config = ConfigManager.load(self.config_path)
        quick_mode_enabled = not config.get("enable_text_refine", True)
        quick_mode_item.set_active(quick_mode_enabled)
        quick_mode_item.connect("toggled", lambda item: self.root.after(0, lambda: self.toggle_quick_mode(item.get_active())))
        menu.append(quick_mode_item)
        self._appindicator_quick_mode_item = quick_mode_item

        # Copy last text
        copy_text_item = Gtk.MenuItem(label="复制最后识别的文本")
        copy_text_item.connect("activate", lambda _: self.root.after(0, self.copy_last_text))
        copy_text_item.set_sensitive(bool(self.state.last_text))
        menu.append(copy_text_item)
        self._appindicator_copy_text_item = copy_text_item

        # Preset submenu
        preset_menu_item = Gtk.MenuItem(label="切换 Preset")
        preset_submenu = Gtk.Menu()

        presets = ["default", "formal", "meeting", "summary", "technical"]
        current_preset = config.get("refine_preset", "default")

        # Create radio group for presets
        radio_group = None
        for preset in presets:
            preset_item = Gtk.RadioMenuItem(group=radio_group, label=preset)
            if radio_group is None:
                radio_group = preset_item
            if preset == current_preset:
                preset_item.set_active(True)
            preset_item.connect("activate", lambda item, p=preset: self.root.after(0, lambda: self.switch_preset(p)) if item.get_active() else None)
            preset_submenu.append(preset_item)

        preset_menu_item.set_submenu(preset_submenu)
        menu.append(preset_menu_item)

        # Settings
        settings_item = Gtk.MenuItem(label="Settings...")
        settings_item.connect("activate", lambda _: self.root.after(0, self.open_settings))
        menu.append(settings_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Quit
        quit_item = Gtk.MenuItem(label="Quit")
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
                label = f"Status: {status}"

                def _gtk_update():
                    if status_item is not None:
                        status_item.set_label(label)
                    # Update last text item
                    last_text_item = getattr(self, '_appindicator_last_text_item', None)
                    if last_text_item is not None:
                        last_text = self.state.last_text[:50] + "..." if len(self.state.last_text) > 50 else self.state.last_text
                        last_text_label = f"Last: {last_text}" if last_text else "Last: (无)"
                        last_text_item.set_label(last_text_label)
                    # Update performance stats item
                    perf_item = getattr(self, '_appindicator_perf_item', None)
                    if perf_item is not None:
                        if self.state.last_total_ms > 0:
                            perf_label = f"Perf: {self.state.last_total_ms:.0f}ms (录:{self.state.last_record_ms:.0f} 识:{self.state.last_transcribe_ms:.0f} 优:{self.state.last_refine_ms:.0f})"
                        else:
                            perf_label = "Perf: (无数据)"
                        perf_item.set_label(perf_label)
                    # Update copy text item sensitivity
                    copy_text_item = getattr(self, '_appindicator_copy_text_item', None)
                    if copy_text_item is not None:
                        copy_text_item.set_sensitive(bool(self.state.last_text))
                    try:
                        indicator.set_icon(icon_path)
                    except Exception:
                        pass

                self._glib.idle_add(_gtk_update)

    def quit(self) -> None:
        self.backend.stop()
        self.overlay.shutdown()

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
            if hasattr(self, '_glib') and hasattr(self, '_gtk'):
                try:
                    self._glib.idle_add(self._gtk.main_quit)
                except Exception:
                    pass

        self.root.quit()
        self.root.destroy()


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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
    args = build_parser().parse_args()
    app = TrayApp(args)
    app.run()


if __name__ == "__main__":
    main()
