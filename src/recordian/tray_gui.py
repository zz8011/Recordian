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
from recordian.exceptions import CommitError, ConfigError
from recordian.preset_manager import PresetManager
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
        self._gtk_settings_window: Any = None

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

        # 显示通知反馈
        try:
            from .linux_notify import notify
            notify(f"已切换到{mode_text}", title="Recordian")
        except Exception:  # noqa: BLE001
            pass  # 通知失败不影响功能

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
        current_notify_backend = str(current.get("notify_backend", "auto"))
        if current_notify_backend not in {"none", "auto", "notify-send", "stdout"}:
            current_notify_backend = "auto"

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

            # 状态标签
            status_label = Gtk.Label()
            status_label.set_xalign(0.0)
            status_label.set_opacity(0.75)
            root_box.pack_start(status_label, False, False, 0)

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

                    # 更新配置
                    current["asr_context"] = context_text
                    ConfigManager.save(self.config_path, current)

                    status_label.set_markup('<span foreground="green">✓ 保存成功！</span>')
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
            row = _add_field(sec_asr, row, key="qwen_model", label="Qwen ASR 模型路径", value=current.get("qwen_model", ""))
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
                value=current.get("asr_endpoint", "http://localhost:8000/transcribe"),
                hint="仅 asr_provider=http-cloud 时生效",
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
            row = _add_field(
                sec_refine,
                row,
                key="enable_text_refine",
                label="启用文本精炼",
                value=current.get("enable_text_refine", False),
                kind="bool",
                default_bool=False,
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
            row = _add_field(sec_refine, row, key="refine_preset", label="精炼预设", value=current.get("refine_preset", "default"))
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

            preset_select_label = Gtk.Label(label="选择预设")
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

            status_label = Gtk.Label(label="已载入当前配置。")
            status_label.set_xalign(0.0)
            status_label.set_opacity(0.78)
            status_label_ref["widget"] = status_label

            def _set_status(text: str) -> None:
                status_label.set_text(text)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            footer.pack_start(status_label, True, True, 0)
            root_box.pack_start(footer, False, False, 0)

            def _set_refine_preset_entry(name: str) -> None:
                target = entries.get("refine_preset")
                if not target:
                    return
                kind, widget = target
                if kind == "entry" and hasattr(widget, "set_text"):
                    widget.set_text(name)
                elif kind == "combo" and hasattr(widget, "set_active"):
                    model = widget.get_model()
                    if model is None:
                        return
                    active_idx = -1
                    for idx, row in enumerate(model):
                        if str(row[0]) == name:
                            active_idx = idx
                            break
                    widget.set_active(active_idx)

            def _read_refine_preset_value() -> str:
                target = entries.get("refine_preset")
                if not target:
                    return ""
                kind, widget = target
                if kind == "entry" and hasattr(widget, "get_text"):
                    return str(widget.get_text()).strip()
                if kind == "combo" and hasattr(widget, "get_active_text"):
                    text = widget.get_active_text()
                    return str(text).strip() if text is not None else ""
                return ""

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

                target = prefer or _read_refine_preset_value() or str(current.get("refine_preset", "default"))
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
                    _set_refine_preset_entry(name)
                    preset_name_entry.set_text("")
                    _set_status(f"已新建预设：{name}")
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
                    if str(_get_value("refine_preset")).strip() == selected:
                        _set_refine_preset_entry(fallback)
                    _set_status(f"已删除预设：{selected}")
                except FileNotFoundError:
                    _set_status("预设文件不存在")
                except Exception as exc:  # noqa: BLE001
                    _set_status(f"删除失败：{exc}")

            def _set_selected_as_current(*_args: object) -> None:
                selected = preset_combo.get_active_text()
                if not selected:
                    _set_status("请先选择预设")
                    return
                _set_refine_preset_entry(selected)
                _set_status(f"当前精炼预设已设为：{selected}")

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

            def _save(*, restart_backend: bool) -> None:
                def _parse_int_field(key: str, default: int) -> int:
                    raw = str(_get_value(key)).strip()
                    return int(raw) if raw else default

                def _parse_float_field(key: str, default: float) -> float:
                    raw = str(_get_value(key)).strip()
                    return float(raw) if raw else default

                try:
                    record_format = str(_get_value("record_format")).strip().lower() or "ogg"
                    if record_format == "mp3":
                        record_format = "ogg"
                    if record_format not in {"ogg", "wav"}:
                        record_format = "ogg"

                    record_backend = str(_get_value("record_backend")).strip() or "auto"
                    if record_backend == "ffmpeg":
                        record_backend = "ffmpeg-pulse"
                    if record_backend not in {"auto", "ffmpeg-pulse", "arecord"}:
                        record_backend = "auto"

                    refine_provider = str(_get_value("refine_provider")).strip() or "local"
                    if refine_provider == "llama.cpp":
                        refine_provider = "llamacpp"
                    if refine_provider not in {"local", "cloud", "llamacpp"}:
                        refine_provider = "local"

                    commit_backend = str(_get_value("commit_backend")).strip() or "auto"
                    if commit_backend == "pynput":
                        commit_backend = "auto"
                    if commit_backend not in {"none", "auto", "wtype", "xdotool", "xdotool-clipboard", "stdout"}:
                        commit_backend = "auto"

                    payload = {
                        "hotkey": str(_get_value("hotkey")).strip(),
                        "stop_hotkey": str(_get_value("stop_hotkey")).strip(),
                        "toggle_hotkey": str(_get_value("toggle_hotkey")).strip(),
                        "exit_hotkey": str(current.get("exit_hotkey", "<ctrl>+<alt>+q")).strip(),
                        "cooldown_ms": _parse_int_field("cooldown_ms", 300),
                        "trigger_mode": str(_get_value("trigger_mode")).strip() or "ptt",
                        "notify_backend": str(_get_value("notify_backend")).strip() or "auto",
                        "duration": _parse_float_field("duration", 4.0),
                        "sample_rate": _parse_int_field("sample_rate", 16000),
                        "channels": _parse_int_field("channels", 1),
                        "input_device": str(_get_value("input_device")).strip() or "default",
                        "record_format": record_format,
                        "record_backend": record_backend,
                        "commit_backend": commit_backend,
                        "asr_provider": str(_get_value("asr_provider")).strip() or "qwen-asr",
                        "qwen_model": str(_get_value("qwen_model")).strip(),
                        "qwen_language": str(_get_value("qwen_language")).strip() or "Chinese",
                        "qwen_max_new_tokens": _parse_int_field("qwen_max_new_tokens", 1024),
                        "asr_context_preset": str(_get_value("asr_context_preset")).strip(),
                        "asr_context": str(_get_value("asr_context")).strip(),
                        "asr_endpoint": str(_get_value("asr_endpoint")).strip() or "http://localhost:8000/transcribe",
                        "asr_timeout_s": _parse_float_field("asr_timeout_s", 30.0),
                        "device": str(_get_value("device")).strip() or "cuda",
                        "enable_text_refine": bool(_get_value("enable_text_refine")),
                        "refine_provider": refine_provider,
                        "refine_preset": str(_get_value("refine_preset")).strip() or "default",
                        "refine_model": str(_get_value("refine_model")).strip(),
                        "refine_device": str(_get_value("refine_device")).strip() or "cuda",
                        "refine_n_gpu_layers": _parse_int_field("refine_n_gpu_layers", -1),
                        "refine_max_tokens": _parse_int_field("refine_max_tokens", 512),
                        "enable_thinking": bool(_get_value("enable_thinking")),
                        "refine_api_base": str(_get_value("refine_api_base")).strip(),
                        "refine_api_key": str(_get_value("refine_api_key")).strip(),
                        "refine_api_model": str(_get_value("refine_api_model")).strip(),
                        "warmup": bool(_get_value("warmup")),
                        "debug_diagnostics": bool(_get_value("debug_diagnostics")),
                        "hub": current.get("hub", "ms"),
                        "remote_code": current.get("remote_code", ""),
                        "hotword": current.get("hotword", []),
                        "enable_streaming_refine": current.get("enable_streaming_refine", False),
                    }
                    ConfigManager.save(self.config_path, payload)
                except ValueError as exc:
                    status_label.set_text(f"保存失败：数值格式不正确 ({exc})")
                    return
                except Exception as exc:  # noqa: BLE001
                    status_label.set_text(f"保存失败：{exc}")
                    return

                if restart_backend:
                    status_label.set_text(f"已保存并重启后端 ({self.config_path})")
                    self.root.after(0, self.backend.restart)
                else:
                    status_label.set_text(f"已保存 ({self.config_path})")

            btn_save = Gtk.Button(label="仅保存")
            btn_save.connect("clicked", lambda *_: _save(restart_backend=False))
            footer.pack_end(btn_save, False, False, 0)

            btn_save_restart = Gtk.Button(label="保存并重启")
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

        # 预设子菜单
        preset_menu_item = Gtk.MenuItem(label="切换预设")
        preset_submenu = Gtk.Menu()

        presets = ["default", "formal", "meeting", "summary", "technical"]
        preset_labels = {
            "default": "默认",
            "formal": "正式",
            "meeting": "会议",
            "summary": "总结",
            "technical": "技术",
        }
        current_preset = config.get("refine_preset", "default")

        # Create radio group for presets
        radio_group = None
        for preset in presets:
            preset_item = Gtk.RadioMenuItem(group=radio_group, label=preset_labels.get(preset, preset))
            if radio_group is None:
                radio_group = preset_item
            if preset == current_preset:
                preset_item.set_active(True)
            preset_item.connect("activate", lambda item, p=preset: self.root.after(0, lambda: self.switch_preset(p)) if item.get_active() else None)
            preset_submenu.append(preset_item)

        preset_menu_item.set_submenu(preset_submenu)
        menu.append(preset_menu_item)

        # 常用词管理
        context_item = Gtk.MenuItem(label="常用词管理...")
        context_item.connect("activate", lambda _: self.root.after(0, self.open_context_editor))
        menu.append(context_item)

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
    args = build_parser().parse_args()
    app = TrayApp(args)
    app.run()


if __name__ == "__main__":
    main()
