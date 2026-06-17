from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from recordian.config import ConfigManager
from recordian.preset_manager import PresetManager
from recordian.refine_model_discovery import fetch_model_list
from recordian.runtime_config import normalize_runtime_config
from recordian.setting_effects import combined_setting_effect, effect_label, effect_status_message
from recordian.tray_settings_utils import KEY_LABEL_MAP
from recordian.tray_utils import save_config_changes
from recordian.voice_wake import DEFAULT_WAKE_KEYWORD_THRESHOLD, DEFAULT_WAKE_NUM_THREADS

logger = logging.getLogger(__name__)

HOTKEY_CAPTURE_FIELDS = {"hotkey", "stop_hotkey", "toggle_hotkey"}


def coerce_bool(value: object, *, default: bool) -> bool:
    """Coerce a value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return parse_bool(value, default=default)
    return default


def parse_bool(value: str, *, default: bool) -> bool:
    """Parse a string as a boolean."""
    token = value.strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def normalize_hotkey_token(raw: str) -> str:
    """Normalize a raw key name to a standard token."""
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


def format_hotkey_spec(*, modifiers: set[str], key: str) -> str:
    """Format a hotkey spec from modifiers and key."""
    if key in {"ctrl_l", "ctrl_r"}:
        modifiers.discard("ctrl")
    elif key in {"alt_l", "alt_r", "alt_gr"}:
        modifiers.discard("alt")
    elif key in {"shift_l", "shift_r"}:
        modifiers.discard("shift")
    elif key in {"cmd_l", "cmd_r"}:
        modifiers.discard("cmd")

    HOTKEY_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd", "menu")
    parts: list[str] = [mod for mod in HOTKEY_MODIFIER_ORDER if mod in modifiers]
    if key and key not in parts:
        parts.append(key)
    if not parts:
        return ""
    return "+".join(f"<{part}>" for part in parts)


def build_gtk_hotkey_spec(event: object, gdk: Any) -> str:
    """Build a hotkey spec from a GTK key-press event."""
    keyval = getattr(event, "keyval", None)
    if keyval is None:
        return ""
    key_name = gdk.keyval_name(keyval)
    if not key_name:
        return ""
    key = normalize_hotkey_token(key_name)
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
    return format_hotkey_spec(modifiers=modifiers, key=key)


def load_hotkey_default_config(*, include_sound_defaults: bool) -> dict[str, Any]:
    """Load the default hotkey config from the hotkey parser."""
    from recordian.hotkey_dictate import build_parser as build_hotkey_parser

    parser = build_hotkey_parser()
    defaults = vars(parser.parse_args([]))
    return normalize_runtime_config(
        defaults,
        include_sound_defaults=include_sound_defaults,
        allow_auto_fallback_commit=False,
    )


def open_settings_gtk(
    app: Any,
    *,
    current: dict[str, Any],
    current_record_backend: str,
    current_record_format: str,
    current_refine_provider: str,
    current_commit_backend: str,
    current_enable_thinking: object,
    current_notify_backend: str,
) -> None:
    """Open the GTK settings window for Recordian."""
    Gtk = app._gtk
    GLib = app._glib
    config_path = app.config_path

    def _on_gtk_thread() -> bool:
        if app._gtk_settings_window is not None:
            try:
                app._gtk_settings_window.present()
                return False
            except Exception:
                app._gtk_settings_window = None

        win = Gtk.Window(title="Recordian 设置")
        win.set_default_size(900, 760)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_keep_above(True)
        app._gtk_settings_window = win

        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root_box.set_border_width(12)
        win.add(root_box)

        title_label = Gtk.Label(label="Recordian 设置")
        title_label.set_xalign(0.0)
        title_label.set_markup("<b>Recordian 设置</b>")
        root_box.pack_start(title_label, False, False, 0)

        config_label = Gtk.Label(label=f"配置文件: {config_path}")
        config_label.set_xalign(0.0)
        config_label.set_opacity(0.75)
        root_box.pack_start(config_label, False, False, 0)

        notebook = Gtk.Notebook()
        root_box.pack_start(notebook, True, True, 0)

        entries: dict[str, tuple[str, Any]] = {}
        status_label_ref: dict[str, Any] = {"widget": None}
        try:
            from gi.repository import Gdk, GLib  # type: ignore
        except Exception:
            Gdk = None
            GLib = None

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
                widget.set_active(coerce_bool(value, default=default_bool))
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
            elif kind == "file":
                row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row_box.set_hexpand(True)
                entry = Gtk.Entry()
                entry.set_text(str(value))
                entry.set_hexpand(True)
                chooser = Gtk.FileChooserButton(title="选择文件")
                current_path = str(value).strip()
                if current_path:
                    try:
                        chooser.set_filename(str(Path(current_path).expanduser()))
                    except Exception:
                        pass
                chooser.connect("file-set", lambda w: entry.set_text(w.get_filename() or ""))
                row_box.pack_start(entry, True, True, 0)
                row_box.pack_start(chooser, False, False, 0)
                grid.attach(row_box, 1, row, 1, 1)
                entries[key] = ("entry", entry)
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
                        spec = build_gtk_hotkey_spec(event, Gdk)
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
                if stem.startswith("asr-"):
                    continue
                names.append(stem)
            return names

        tab_basic = _create_tab("基础")
        tab_asr = _create_tab("ASR")
        tab_refine = _create_tab("文本精炼")
        tab_presets = _create_tab("预设管理")
        tab_remote = _create_tab("远程粘贴")
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
        row = _add_field(sec_asr, row, key="qwen_max_new_tokens", label="Qwen Max Tokens", value=current.get("qwen_max_new_tokens", 8192))
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
            key="asr_realtime_endpoint",
            label="HTTP ASR Realtime",
            value=current.get("asr_realtime_endpoint", ""),
            hint="仅 asr_provider=http-cloud 时生效。实时增量 ASR 示例：http://192.168.5.111:40002",
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
        current_preset_hint = Gtk.Label(label="切换请到「预设管理」页，或直接使用托盘菜单。")
        current_preset_hint.set_xalign(0.0)
        current_preset_hint.set_opacity(0.75)
        current_preset_box.pack_start(current_preset_hint, False, False, 0)
        sec_refine.attach(current_preset_box, 1, row, 1, 1)
        row += 1

        # Cloud provider fields: API base, key, and model dropdown
        row = _add_field(sec_refine, row, key="refine_api_base", label="云端 API Base", value=current.get("refine_api_base", ""))
        row = _add_field(sec_refine, row, key="refine_api_key", label="云端 API Key", value=current.get("refine_api_key", ""), secret=True)

        # Cloud model selection: combo + refresh button
        model_label = Gtk.Label(label="云端 API 模型")
        model_label.set_xalign(0.0)
        model_label.set_yalign(0.0)
        sec_refine.attach(model_label, 0, row, 1, 1)

        model_combo = Gtk.ComboBoxText()
        model_combo.set_hexpand(True)
        current_model = str(current.get("refine_api_model", "")).strip()
        model_combo.append_text(current_model if current_model else "")
        if current_model:
            model_combo.set_active(0)
        else:
            model_combo.append_text("")
            model_combo.set_active(1)

        refresh_btn = Gtk.Button(label="刷新模型列表")
        refresh_status = Gtk.Label()
        refresh_status.set_xalign(0.0)
        refresh_status.set_opacity(0.75)

        def _refresh_models(*_args: object) -> None:
            api_base = str(current.get("refine_api_base", "")).strip()
            api_key = str(current.get("refine_api_key", "")).strip()
            if not api_base:
                refresh_status.set_text("请先填写 API Base")
                return
            refresh_status.set_text("正在获取模型列表...")
            refresh_btn.set_sensitive(False)

            def _do_fetch() -> None:
                try:
                    models = fetch_model_list(api_base, api_key, timeout_s=8.0)
                except Exception as exc:
                    models = []
                    error_msg = str(exc)
                else:
                    error_msg = ""

                def _apply() -> None:
                    model_combo.remove_all()
                    if models:
                        for m in models:
                            model_combo.append_text(m)
                        # Try to keep current selection if still available
                        current = current_model
                        if current in models:
                            model_combo.set_active(models.index(current))
                        else:
                            model_combo.set_active(0)
                        refresh_status.set_text(f"已获取 {len(models)} 个模型")
                    else:
                        model_combo.append_text(current_model if current_model else "")
                        model_combo.set_active(0)
                        refresh_status.set_text(f"获取失败: {error_msg or '无可用模型'}")
                    refresh_btn.set_sensitive(True)

                # Schedule on GTK main thread
                if GLib is not None:
                    GLib.idle_add(_apply)
                else:
                    _apply()

            import threading
            threading.Thread(target=_do_fetch, daemon=True).start()

        refresh_btn.connect("clicked", _refresh_models)

        model_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        model_box.set_hexpand(True)
        model_box.pack_start(model_combo, True, True, 0)
        model_box.pack_start(refresh_btn, False, False, 0)

        model_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        model_vbox.set_hexpand(True)
        model_vbox.pack_start(model_box, True, True, 0)
        model_vbox.pack_start(refresh_status, False, False, 0)
        sec_refine.attach(model_vbox, 1, row, 1, 1)
        entries["refine_api_model"] = ("combo", model_combo)
        row += 1

        # Local/llamacpp provider fields
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
        row = _add_field(
            sec_refine,
            row,
            key="capture_refine_samples",
            label="记录精炼样本",
            value=current.get("capture_refine_samples", False),
            kind="bool",
            default_bool=False,
            hint="每次口述保存一轮 ASR 和二轮精炼结果，便于后续对比调参。",
        )
        row = _add_field(
            sec_refine,
            row,
            key="capture_refine_samples_path",
            label="样本文件路径",
            value=current.get("capture_refine_samples_path", "~/.local/share/recordian/refine-samples.jsonl"),
            hint="JSONL 文件；每行一条样本记录。",
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

        sec_remote = _create_section(tab_remote, "远程粘贴")
        row = 0
        row = _add_field(
            sec_remote,
            row,
            key="enable_remote_paste",
            label="启用远程粘贴",
            value=current.get("enable_remote_paste", False),
            kind="bool",
            default_bool=False,
            hint="默认关闭。开启后会把最终文本发送到远端 Recordian paste agent。",
        )
        row = _add_field(
            sec_remote,
            row,
            key="remote_paste_host",
            label="远程主机",
            value=current.get("remote_paste_host", ""),
            hint="仅在启用远程粘贴时生效，例如 192.168.5.111",
        )
        row = _add_field(
            sec_remote,
            row,
            key="remote_paste_port",
            label="远程端口",
            value=current.get("remote_paste_port", 24872),
            hint="仅在启用远程粘贴时生效，默认 24872",
        )
        row = _add_field(
            sec_remote,
            row,
            key="remote_paste_timeout_s",
            label="远程超时 (s)",
            value=current.get("remote_paste_timeout_s", 3.0),
            hint="远程连接超时秒数，默认 3.0",
        )
        row = _add_field(
            sec_remote,
            row,
            key="remote_paste_mode",
            label="远程传输模式",
            value=current.get("remote_paste_mode", "direct"),
            kind="combo",
            options=("direct", "shared-clipboard"),
            hint="direct: 直接把文本发给远端 agent；shared-clipboard: 利用 DeskFlow/Synergy 共享剪贴板传输，远端只执行粘贴快捷键。",
        )
        row = _add_field(
            sec_remote,
            row,
            key="remote_paste_sync_wait_s",
            label="共享剪贴板等待 (s)",
            value=current.get("remote_paste_sync_wait_s", 0.35),
            hint="仅 shared-clipboard 模式生效。等待 DeskFlow 把本机剪贴板同步到远端后再触发粘贴。",
        )
        row += 1
        row = _add_field(
            sec_remote,
            row,
            key="remote_paste_follow_deskflow_active_screen",
            label="按 DeskFlow 活动屏幕路由",
            value=current.get("remote_paste_follow_deskflow_active_screen", False),
            kind="bool",
            default_bool=False,
            hint="开启后：鼠标在远端屏幕时只远端上屏；否则只本地上屏。",
        )
        row = _add_field(
            sec_remote,
            row,
            key="deskflow_active_screen_path",
            label="DeskFlow 状态文件",
            value=current.get("deskflow_active_screen_path", "~/.local/state/deskflow/active_screen.json"),
            hint="优先读取这个 active_screen.json；如果不存在，再尝试解析 DeskFlow 日志。",
        )
        row = _add_field(
            sec_remote,
            row,
            key="deskflow_log_path",
            label="DeskFlow 日志文件",
            value=current.get("deskflow_log_path", ""),
            hint="可选。状态文件不可用时，解析日志里最新的 switch from/to 记录。",
        )
        _add_field(
            sec_remote,
            row,
            key="remote_paste_screen_name",
            label="远端屏幕名",
            value=current.get("remote_paste_screen_name", ""),
            hint="DeskFlow 配置里的远端 screen 名；命中该屏幕时才会走远端上屏。",
        )

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
            key="enable_streaming_commit",
            label="流式上屏",
            value=current.get("enable_streaming_commit", False),
            kind="bool",
            default_bool=False,
            hint="关闭时保持当前一次性上屏；开启后按模型流式结果增量上屏。",
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
            key="wake_owner_threshold",
            label="主人声纹阈值",
            value=current.get("wake_owner_threshold", 0.72),
            hint="0~1，越高越严格（建议 0.68~0.80）",
        )
        row = _add_field(
            sec_wake_main,
            row,
            key="wake_owner_window_s",
            label="声纹分析窗口 (s)",
            value=current.get("wake_owner_window_s", 1.6),
            hint="唤醒后回看最近音频时长",
        )
        row = _add_field(
            sec_wake_main,
            row,
            key="wake_owner_silence_extend_s",
            label="主人静音延长 (s)",
            value=current.get("wake_owner_silence_extend_s", 0.5),
            hint="识别为主人时延长静音阈值，避免停顿被打断",
        )
        row = _add_field(
            sec_wake_main,
            row,
            key="wake_owner_profile",
            label="主人声纹特征文件",
            value=current.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json"),
            hint="JSON 文件路径，可备份/迁移",
        )
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
        sound_on_label = Gtk.Label(label="开始音效路径")
        sound_on_label.set_xalign(0.0)
        sec_wake_main.attach(sound_on_label, 0, row, 1, 1)

        sound_on_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sound_on_entry = Gtk.Entry()
        sound_on_entry.set_text(str(current.get("sound_on_path", "")))
        sound_on_entry.set_hexpand(True)
        sound_on_entry.set_placeholder_text("支持 mp3/wav")
        sound_on_chooser = Gtk.FileChooserButton(title="选择开始音效")
        sound_on_val = str(current.get("sound_on_path", "")).strip()
        if sound_on_val and Path(sound_on_val).expanduser().exists():
            sound_on_chooser.set_filename(str(Path(sound_on_val).expanduser()))
        sound_on_chooser.connect("file-set", lambda w: sound_on_entry.set_text(w.get_filename() or ""))
        sound_on_box.pack_start(sound_on_entry, True, True, 0)
        sound_on_box.pack_start(sound_on_chooser, False, False, 0)
        sec_wake_main.attach(sound_on_box, 1, row, 1, 1)
        entries["sound_on_path"] = ("entry", sound_on_entry)
        row += 1

        sound_off_label = Gtk.Label(label="结束音效路径")
        sound_off_label.set_xalign(0.0)
        sec_wake_main.attach(sound_off_label, 0, row, 1, 1)

        sound_off_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sound_off_entry = Gtk.Entry()
        sound_off_entry.set_text(str(current.get("sound_off_path", "")))
        sound_off_entry.set_hexpand(True)
        sound_off_entry.set_placeholder_text("支持 mp3/wav")
        sound_off_chooser = Gtk.FileChooserButton(title="选择结束音效")
        sound_off_val = str(current.get("sound_off_path", "")).strip()
        if sound_off_val and Path(sound_off_val).expanduser().exists():
            sound_off_chooser.set_filename(str(Path(sound_off_val).expanduser()))
        sound_off_chooser.connect("file-set", lambda w: sound_off_entry.set_text(w.get_filename() or ""))
        sound_off_box.pack_start(sound_off_entry, True, True, 0)
        sound_off_box.pack_start(sound_off_chooser, False, False, 0)
        sec_wake_main.attach(sound_off_box, 1, row, 1, 1)
        entries["sound_off_path"] = ("entry", sound_off_entry)
        row += 1

        wake_model_dir = Path(__file__).parent.parent.parent / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        # Fields hidden from UI but preserved in save payload for backward compatibility
        _HIDDEN_WAKE_FIELDS = {
            "wake_use_semantic_gate",
            "wake_semantic_probe_interval_s",
            "wake_semantic_window_s",
            "wake_semantic_end_silence_s",
            "wake_semantic_min_chars",
            "wake_semantic_timeout_ms",
        }

        sec_wake_model = _create_section(tab_wake, "模型与阈值")
        row = 0
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
        _add_field(
            sec_wake_model,
            row,
            key="wake_keyword_score",
            label="关键词分数",
            value=current.get("wake_keyword_score", 1.5),
        )

        sec_wake_advanced = _create_section(tab_wake, "高级调优")
        row = 0
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_encoder",
            label="Encoder ONNX",
            value=current.get("wake_encoder", str(wake_model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx")),
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_decoder",
            label="Decoder ONNX",
            value=current.get("wake_decoder", str(wake_model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx")),
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_joiner",
            label="Joiner ONNX",
            value=current.get("wake_joiner", str(wake_model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx")),
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_tokens",
            label="Tokens 文件",
            value=current.get("wake_tokens", str(wake_model_dir / "tokens.txt")),
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_tokens_type",
            label="Tokens 类型",
            value=current.get("wake_tokens_type", "ppinyin"),
            kind="combo",
            options=("ppinyin", "cjkchar", "bpe", "fpinyin"),
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_keywords_file",
            label="关键词文件（可选）",
            value=current.get("wake_keywords_file", ""),
            hint="留空自动由前缀+名字生成",
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_sample_rate",
            label="采样率",
            value=current.get("wake_sample_rate", 16000),
        )
        row = _add_field(
            sec_wake_advanced,
            row,
            key="wake_keyword_threshold",
            label="关键词阈值",
            value=current.get("wake_keyword_threshold", DEFAULT_WAKE_KEYWORD_THRESHOLD),
        )
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
        _add_field(
            sec_wake_advanced,
            row,
            key="wake_speech_confirm_s",
            label="开口确认时长 (s)",
            value=current.get("wake_speech_confirm_s", 0.18),
            hint="累计语音证据达到该时长，判定已开口",
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
                app._update_tray_menu()
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
                        app.switch_preset(fallback)
                        _set_status(f"已删除预设：{selected}；当前预设已切换为：{fallback}")
                    else:
                        _set_status(f"已删除预设：{selected}；当前没有可用预设。")
                else:
                    _set_status(f"已删除预设：{selected}")
                app._update_tray_menu()
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
                app.switch_preset(str(selected))
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

        btn_record_owner_sample.connect("clicked", lambda *_: app.open_speaker_enrollment_wizard())

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
                latest_config = ConfigManager.load(config_path)
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
                    "enable_streaming_commit": bool(_get_value("enable_streaming_commit")),
                    "asr_provider": str(_get_value("asr_provider")).strip() or str(current.get("asr_provider", "qwen-asr")),
                    "qwen_model": str(_get_value("qwen_model")).strip(),
                    "qwen_language": str(_get_value("qwen_language")).strip() or str(current.get("qwen_language", "Chinese")),
                    "qwen_max_new_tokens": _parse_int_field("qwen_max_new_tokens", int(current.get("qwen_max_new_tokens", 8192))),
                    "asr_context_preset": str(_get_value("asr_context_preset")).strip(),
                    "asr_context": str(_get_value("asr_context")).strip(),
                    "asr_endpoint": str(_get_value("asr_endpoint")).strip() or str(
                        current.get("asr_endpoint", "http://127.0.0.1:8000/v1/audio/transcriptions")
                    ),
                    "asr_realtime_endpoint": str(_get_value("asr_realtime_endpoint")).strip(),
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
                    "capture_refine_samples": bool(_get_value("capture_refine_samples")),
                    "capture_refine_samples_path": str(_get_value("capture_refine_samples_path")).strip(),
                    "enable_remote_paste": bool(_get_value("enable_remote_paste")),
                    "remote_paste_host": str(_get_value("remote_paste_host")).strip(),
                    "remote_paste_port": _parse_int_field(
                        "remote_paste_port",
                        int(current.get("remote_paste_port", 24872)),
                    ),
                    "remote_paste_timeout_s": _parse_float_field(
                        "remote_paste_timeout_s",
                        float(current.get("remote_paste_timeout_s", 3.0)),
                    ),
                    "remote_paste_mode": str(_get_value("remote_paste_mode")).strip() or str(
                        current.get("remote_paste_mode", "direct")
                    ),
                    "remote_paste_sync_wait_s": _parse_float_field(
                        "remote_paste_sync_wait_s",
                        float(current.get("remote_paste_sync_wait_s", 0.35)),
                    ),
                    "remote_paste_follow_deskflow_active_screen": bool(
                        _get_value("remote_paste_follow_deskflow_active_screen")
                    ),
                    "deskflow_active_screen_path": str(_get_value("deskflow_active_screen_path")).strip()
                    or str(current.get("deskflow_active_screen_path", "~/.local/state/deskflow/active_screen.json")),
                    "deskflow_log_path": str(_get_value("deskflow_log_path")).strip(),
                    "remote_paste_screen_name": str(_get_value("remote_paste_screen_name")).strip(),
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
                    # Hidden semantic gate settings — preserved from latest config, not shown in UI
                    "wake_use_semantic_gate": bool(latest_config.get("wake_use_semantic_gate", False)),
                    "wake_semantic_probe_interval_s": float(cast(Any, latest_config.get("wake_semantic_probe_interval_s", 0.45))),
                    "wake_semantic_window_s": float(cast(Any, latest_config.get("wake_semantic_window_s", 1.2))),
                    "wake_semantic_end_silence_s": float(cast(Any, latest_config.get("wake_semantic_end_silence_s", 1.0))),
                    "wake_semantic_min_chars": int(cast(Any, latest_config.get("wake_semantic_min_chars", 1))),
                    "wake_semantic_timeout_ms": int(cast(Any, latest_config.get("wake_semantic_timeout_ms", 1200))),
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
                payload["wake_no_speech_timeout_s"] = max(0.0, cast(float, payload["wake_no_speech_timeout_s"]))
                payload["wake_speech_confirm_s"] = max(0.0, cast(float, payload["wake_speech_confirm_s"]))
                payload["wake_pre_vad_enter_frames"] = max(1, cast(int, payload["wake_pre_vad_enter_frames"]))
                payload["wake_pre_vad_hangover_ms"] = max(0, cast(int, payload["wake_pre_vad_hangover_ms"]))
                payload["wake_pre_roll_ms"] = max(0, cast(int, payload["wake_pre_roll_ms"]))
                payload["wake_decode_budget_per_cycle"] = max(1, cast(int, payload["wake_decode_budget_per_cycle"]))
                payload["wake_decode_budget_per_sec"] = max(1.0, cast(float, payload["wake_decode_budget_per_sec"]))
                payload["wake_owner_threshold"] = min(0.99, max(0.0, cast(float, payload["wake_owner_threshold"])))
                payload["wake_owner_window_s"] = max(0.6, cast(float, payload["wake_owner_window_s"]))
                payload["wake_owner_silence_extend_s"] = max(0.0, cast(float, payload["wake_owner_silence_extend_s"]))
                effect, restarted, changed_keys = save_config_changes(
                    config_path,
                    payload,
                    apply_now=restart_backend,
                    restart_callback=lambda: app.root.after(0, app.backend.restart),
                )
            except ValueError as exc:
                status_label.set_text(f"保存失败：数值格式不正确 ({exc})")
                return
            except Exception as exc:  # noqa: BLE001
                status_label.set_text(f"保存失败：{exc}")
                return

            app._invalidate_config_cache()
            changed_labels = ", ".join(KEY_LABEL_MAP.get(k, k) for k in changed_keys)
            status_label.set_text(
                f"已保存并重启后端（变更: {changed_labels}）" if restarted else f"{effect_status_message(effect, restarted=restarted)} ({config_path})"
            )
            app._update_tray_menu()

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
            app._gtk_settings_window = None

        win.connect("destroy", _on_destroy)
        win.show_all()
        win.present()
        return False

    GLib.idle_add(_on_gtk_thread)


__all__ = [
    "HOTKEY_CAPTURE_FIELDS",
    "coerce_bool",
    "parse_bool",
    "normalize_hotkey_token",
    "format_hotkey_spec",
    "build_gtk_hotkey_spec",
    "load_hotkey_default_config",
    "open_settings_gtk",
]
