from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from recordian.config import ConfigManager
from recordian.preset_manager import PresetManager
from recordian.recommended_profile import (
    BUSY_SAVE_MESSAGE,
    CONFUCIUS_STOCK_MIGRATION_NOTICE,
    CORRECTION_PROVIDER_CHOICES,
    DICTATION_BUSY_STATUSES,
    LANGUAGE_CHOICES,
    PROVIDER_CHOICES,
    RECOMMENDED_PROFILE_NOTICE,
    RECORD_BACKEND_CHOICES,
    RECORD_FORMAT_CHOICES,
    SAVE_EFFECT_INTRO,
    TRIGGER_MODE_CHOICES,
    confucius_endpoint_problem,
    endpoint_hints_for_provider,
    http_cloud_endpoint_problem,
    migrate_confucius_realtime_endpoint,
    recommended_profile_values,
)
from recordian.refine_model_discovery import fetch_model_list
from recordian.runtime_config import (
    DEFAULT_JEV_TIMEOUT_S,
    DEFAULT_SEMIF_TIMEOUT_S,
    format_contextual_aliases,
    normalize_contextual_aliases,
    normalize_correction_provider,
    normalize_jev_timeout_s,
    normalize_runtime_config,
    normalize_semif_timeout_s,
)
from recordian.setting_effects import SettingEffect, combined_setting_effect, effect_label, effect_status_message
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
        win.set_default_size(780, 680)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_keep_above(True)
        app._gtk_settings_window = win

        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root_box.set_border_width(12)
        win.add(root_box)

        config_label = Gtk.Label(label=f"配置文件: {config_path}")
        config_label.set_xalign(0.0)
        config_label.set_opacity(0.55)
        try:
            from gi.repository import Pango  # type: ignore

            config_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        except Exception:
            pass

        daily_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        daily_page.set_border_width(2)
        daily_scroll = Gtk.ScrolledWindow()
        daily_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        daily_scroll.set_propagate_natural_height(False)
        daily_scroll.add(daily_page)
        daily_scroll.set_vexpand(True)
        root_box.pack_start(daily_scroll, True, True, 0)

        notebook = Gtk.Notebook()
        notebook.set_scrollable(True)
        notebook.set_vexpand(False)
        notebook.set_hexpand(True)
        advanced_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        advanced_box.set_border_width(4)
        advanced_box.set_vexpand(False)
        advanced_box.pack_start(notebook, True, True, 0)
        advanced_box.pack_start(config_label, False, False, 0)
        advanced = Gtk.Expander(label="高级设置")
        advanced.set_expanded(False)
        advanced.set_vexpand(False)
        advanced.set_resize_toplevel(False)
        advanced.add(advanced_box)
        root_box.pack_start(advanced, False, False, 0)

        def _sync_advanced_space(*_args: object) -> None:
            opened = bool(advanced.get_expanded())
            advanced.set_vexpand(opened)
            advanced_box.set_vexpand(opened)
            notebook.set_vexpand(opened)
            for child in notebook.get_children():
                child.set_propagate_natural_height(False)
                child.set_vexpand(opened)
                child.set_min_content_height(220 if opened else 0)
            daily_scroll.set_vexpand(True)

        entries: dict[str, tuple[str, Any]] = {}
        field_rows: dict[str, list[Any]] = {}
        hint_labels: dict[str, Any] = {}
        mapped_ids: dict[str, list[str]] = {}
        status_label_ref: dict[str, Any] = {"widget": None}
        try:
            from gi.repository import Gdk, GLib  # type: ignore
        except Exception:
            Gdk = None
            GLib = None

        def _create_tab(name: str) -> Any:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            page.set_border_width(10)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_propagate_natural_height(False)
            scroll.set_min_content_height(0)
            scroll.set_vexpand(False)
            scroll.add(page)
            notebook.append_page(scroll, Gtk.Label(label=name))
            return page

        def _create_section(parent: Any, title: str) -> Any:
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

        def _create_collapsible_section(parent: Any, title: str) -> Any:
            """Section hidden behind a collapsed expander — for rarely-touched tuning fields."""
            expander = Gtk.Expander(label=title)
            expander.set_expanded(False)
            expander.set_margin_top(4)
            expander.set_margin_bottom(6)
            grid = Gtk.Grid()
            grid.set_border_width(10)
            grid.set_column_spacing(12)
            grid.set_row_spacing(6)
            expander.add(grid)
            parent.pack_start(expander, False, False, 0)
            return grid

        def _add_field(
            grid: Any,
            row: int,
            *,
            key: str,
            label: str,
            value: object,
            kind: str = "entry",
            options: tuple[str, ...] = (),
            choices: tuple[tuple[str, str], ...] = (),
            hint: str = "",
            default_bool: bool = False,
            secret: bool = False,
        ) -> int:
            tracked: list[Any] = []
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
                    hint_label.set_line_wrap(True)
                    hint_label.set_max_width_chars(48)
                    hint_label.set_opacity(0.75)
                    left_box.pack_start(hint_label, False, False, 0)
                    hint_labels[key] = hint_label
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
                field_rows[key] = [row_box]
                return row + 1

            label_widget = Gtk.Label(label=label)
            label_widget.set_xalign(0.0)
            label_widget.set_yalign(0.0)
            grid.attach(label_widget, 0, row, 1, 1)
            tracked.append(label_widget)
            next_row = row + 1
            if kind == "mapped":
                ids = [item_id for item_id, _item_label in choices]
                labels = [item_label for _item_id, item_label in choices]
                selected = str(value)
                if selected and selected not in ids:
                    ids.append(selected)
                    labels.append(selected)
                widget = Gtk.ComboBoxText()
                widget.set_hexpand(True)
                for item_label in labels:
                    widget.append_text(item_label)
                if ids:
                    widget.set_active(ids.index(selected) if selected in ids else 0)
                grid.attach(widget, 1, row, 1, 1)
                entries[key] = ("mapped", widget)
                mapped_ids[key] = ids
                tracked.append(widget)
            elif kind == "combo":
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
                tracked.append(widget)
            elif kind == "text":
                widget = Gtk.TextView()
                widget.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                widget.get_buffer().set_text(str(value))
                widget.set_hexpand(True)
                scroll = Gtk.ScrolledWindow()
                scroll.set_hexpand(True)
                scroll.set_min_content_height(72)
                scroll.set_shadow_type(Gtk.ShadowType.IN)
                scroll.add(widget)
                grid.attach(scroll, 1, row, 1, 1)
                entries[key] = ("text", widget)
                tracked.append(scroll)
                tracked.append(widget)
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
                tracked.append(row_box)
            else:
                widget = Gtk.Entry()
                widget.set_text(str(value))
                widget.set_hexpand(True)
                if secret:
                    widget.set_visibility(False)
                    widget.set_invisible_char("●")
                if key in HOTKEY_CAPTURE_FIELDS and Gdk is not None:
                    widget.set_placeholder_text("点击后按下要使用的键")

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
                tracked.append(widget)
            if hint:
                hint_label = Gtk.Label(label=hint)
                hint_label.set_xalign(0.0)
                hint_label.set_line_wrap(True)
                hint_label.set_max_width_chars(52)
                hint_label.set_opacity(0.75)
                grid.attach(hint_label, 1, next_row, 1, 1)
                hint_labels[key] = hint_label
                tracked.append(hint_label)
                next_row += 1
            field_rows[key] = tracked
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

        intro = Gtk.Label(
            label="日常听写。适合中文和英文，在浏览器、微信和编辑器里按住说话即可。"
        )
        intro.set_xalign(0.0)
        intro.set_line_wrap(True)
        intro.set_max_width_chars(52)
        daily_page.pack_start(intro, False, False, 0)

        recommend_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        recommend_button = Gtk.Button(label="使用本机推荐配置")
        recommend_button.set_halign(Gtk.Align.START)
        recommend_hint = Gtk.Label(label="按需填写本机常用设置，保存后生效。常用词和口令会保留。")
        recommend_hint.set_xalign(0.0)
        recommend_hint.set_line_wrap(True)
        recommend_hint.set_max_width_chars(52)
        recommend_hint.set_lines(2)
        recommend_hint.set_opacity(0.75)
        recommend_box.pack_start(recommend_button, False, False, 0)
        recommend_box.pack_start(recommend_hint, False, False, 0)
        daily_page.pack_start(recommend_box, False, False, 0)

        sec_daily_talk = _create_section(daily_page, "说话方式")
        row = 0
        row = _add_field(
            sec_daily_talk,
            row,
            key="trigger_mode",
            label="怎么开始",
            value=current.get("trigger_mode", "ptt"),
            kind="mapped",
            choices=TRIGGER_MODE_CHOICES,
            hint="按住说话：按住键说话，松开结束。点一下开关：按一次开始，再按一次结束。",
        )
        row = _add_field(
            sec_daily_talk,
            row,
            key="hotkey",
            label="按住说话的键",
            value=current.get("hotkey", "<ctrl_r>"),
            hint="推荐右 Ctrl。点进输入框后直接按下要使用的键，Delete 可清空。",
        )
        _add_field(
            sec_daily_talk,
            row,
            key="toggle_hotkey",
            label="开关用的键",
            value=current.get("toggle_hotkey", "<alt_r>"),
            hint="推荐右 Alt。点一下开关时用这一键。",
        )

        sec_daily_asr = _create_section(daily_page, "识别和上屏")
        row = 0
        row = _add_field(
            sec_daily_asr,
            row,
            key="qwen_language",
            label="语言",
            value=current.get("qwen_language", "auto"),
            kind="mapped",
            choices=LANGUAGE_CHOICES,
            hint="自动会在中文和英文之间判断，适合浏览器、微信和编辑器。",
        )
        row = _add_field(
            sec_daily_asr,
            row,
            key="input_device",
            label="麦克风",
            value=current.get("input_device", "default"),
            hint="留空或 default 使用系统当前麦克风。要固定某一只时，填写系统里的设备名。",
        )
        row = _add_field(
            sec_daily_asr,
            row,
            key="enable_streaming_commit",
            label="边说边出字",
            value=current.get("enable_streaming_commit", False),
            kind="bool",
            default_bool=False,
            hint="开：边说边在光标处显示。关：松手后一次性贴上。部分应用只会在松手后贴上。",
        )
        row = _add_field(
            sec_daily_asr,
            row,
            key="auto_hard_enter",
            label="说完自动回车",
            value=current.get("auto_hard_enter", False),
            kind="bool",
            default_bool=False,
            hint="默认关闭。打开后，文字上屏结束会再按一次回车。聊天窗口里容易误发。",
        )
        _add_field(
            sec_daily_asr,
            row,
            key="asr_context",
            label="常用词",
            value=current.get("asr_context", ""),
            hint="人名、产品名、命令都可以，中英文都行，用逗号分隔。推荐配置不会清掉这里。",
        )

        tab_basic = _create_tab("录音细节")
        tab_asr = _create_tab("识别服务")
        tab_refine = _create_tab("文字润色")
        tab_presets = _create_tab("润色风格")
        tab_remote = _create_tab("远程粘贴")
        tab_wake = _create_tab("语音唤醒")
        tab_advanced = _create_tab("上屏和诊断")

        sec_hotkey = _create_section(tab_basic, "其它热键")
        row = 0
        row = _add_field(
            sec_hotkey,
            row,
            key="stop_hotkey",
            label="停止键",
            value=current.get("stop_hotkey", ""),
            hint="留空则沿用原来的停止方式。推荐配置会把它设成右 Ctrl。",
        )
        _add_field(sec_hotkey, row, key="cooldown_ms", label="两次触发间隔 (毫秒)", value=current.get("cooldown_ms", 300))

        sec_record = _create_section(tab_basic, "录音")
        row = 0
        row = _add_field(sec_record, row, key="duration", label="单次最长录音 (秒)", value=current.get("duration", 4.0))
        row = _add_field(
            sec_record,
            row,
            key="record_backend",
            label="录音方式",
            value=current_record_backend,
            kind="mapped",
            choices=RECORD_BACKEND_CHOICES,
        )
        row = _add_field(
            sec_record,
            row,
            key="record_format",
            label="录音格式",
            value=current_record_format,
            kind="mapped",
            choices=RECORD_FORMAT_CHOICES,
            hint="本机 Confucius 推荐 WAV、16000 Hz、单声道。",
        )
        row = _add_field(sec_record, row, key="sample_rate", label="采样率", value=current.get("sample_rate", 16000))
        _add_field(sec_record, row, key="channels", label="声道数", value=current.get("channels", 1))

        sec_asr = _create_section(tab_asr, "识别服务")
        row = 0
        row = _add_field(
            sec_asr,
            row,
            key="asr_provider",
            label="识别方式",
            value=current.get("asr_provider", "qwen-asr"),
            kind="mapped",
            choices=PROVIDER_CHOICES,
            hint="换方式后，下面只显示这个方式真正会用到的项目。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="qwen_model",
            label="模型",
            value=current.get("qwen_model", ""),
            hint="本机 Qwen 填模型位置；网络识别填服务里的模型名。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="qwen_max_new_tokens",
            label="最长输出",
            value=current.get("qwen_max_new_tokens", 8192),
            hint="只对本机 Qwen 有效。本机 Confucius 和网络识别会忽略它，但保存时仍保留原值。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="asr_endpoint",
            label="识别接口",
            value=current.get("asr_endpoint", "http://127.0.0.1:8000/v1/audio/transcriptions"),
            hint="只对网络识别有效。地址以 http:// 或 https:// 开头。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="asr_realtime_endpoint",
            label="实时地址",
            value=current.get("asr_realtime_endpoint", ""),
            hint="流式 Confucius 用 ws:// 或 wss://，自定义路径会保留。网络识别用 http(s) 地址。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="asr_api_key",
            label="访问口令",
            value=current.get("asr_api_key", ""),
            hint="本机 Confucius 和网络识别都会用到。这里不会生成口令。",
            secret=True,
        )
        row = _add_field(
            sec_asr,
            row,
            key="asr_timeout_s",
            label="等待上限 (秒)",
            value=current.get("asr_timeout_s", 30.0),
            hint="流式 Confucius 等待识别结束，以及网络识别的整段请求，都会用到这个上限。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="asr_context_preset",
            label="常用词预设",
            value=current.get("asr_context_preset", ""),
            hint="留空，或填 default、formal、meeting、technical、simple。日常常用词在上面一页。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="enable_semif_correction",
            label="上下文纠词",
            value=current.get("enable_semif_correction", False),
            kind="bool",
            default_bool=False,
            hint="默认关闭。只在已声明的别名上问一次语义角色，失败、超时或拿不准都保留原文。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="correction_provider",
            label="纠词来源",
            value=current.get("correction_provider", "semif"),
            kind="mapped",
            choices=CORRECTION_PROVIDER_CHOICES,
            hint="默认使用本机或内网部署的 SemIf。官方 Jev 复用 jev 的登录，密钥由 jev 管理；选择官方 Jev 无需填写地址。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="semif_endpoint",
            label="SemIf 服务地址",
            value=current.get("semif_endpoint", ""),
            hint="选择本地 SemIf 时使用；切换到官方 Jev 后仍保留此地址。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="semif_timeout_s",
            label="SemIf 等待 (秒)",
            value=current.get("semif_timeout_s", DEFAULT_SEMIF_TIMEOUT_S),
            hint="默认 0.12 秒，必须大于 0 且不超过 0.35 秒。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="jev_timeout_s",
            label="Jev 等待 (秒)",
            value=current.get("jev_timeout_s", DEFAULT_JEV_TIMEOUT_S),
            hint="默认 1.5 秒，不超过 2 秒。这是整句判断的总时间，预览不会等它。",
        )
        row = _add_field(
            sec_asr,
            row,
            key="contextual_aliases",
            label="语境别名",
            value=format_contextual_aliases(current.get("contextual_aliases", [])),
            kind="text",
            hint="每行一条，如 jeff→jev::软件工具。也可以用逗号分隔。只有整句语境明确时才替换，拿不准就保留原文。",
        )
        _add_field(
            sec_asr,
            row,
            key="device",
            label="计算设备",
            value=current.get("device", "cuda"),
            kind="combo",
            options=("cuda", "cpu", "auto"),
            hint="只对本机 Qwen 有效。换成其它识别方式后，这里会藏起来，原值仍会保存。",
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
            hint="关闭后直接使用识别结果。关掉时，下面的润色项目会变灰，已经填写的内容仍会保存。",
        )
        row = _add_field(
            sec_refine,
            row,
            key="refine_provider",
            label="润色方式",
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
        current_preset_hint = Gtk.Label(label="换风格请到「润色风格」页，或用托盘里的「更多」。润色关闭时不能切换。")
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
        field_rows["refine_api_model"] = [model_label, model_vbox]
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
            label="上屏方式",
            value=current_commit_backend,
            kind="combo",
            options=("auto", "fcitx", "wtype", "xdotool", "xdotool-clipboard", "stdout", "none"),
            hint="自动会先用输入法通道，不行再粘贴。",
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
        row = _add_field(
            sec_advanced,
            row,
            key="capture_refine_samples",
            label="记录精炼样本",
            value=current.get("capture_refine_samples", False),
            kind="bool",
            default_bool=False,
            hint="每次口述保存一轮 ASR 和二轮精炼结果，便于后续对比调参。",
        )
        row = _add_field(
            sec_advanced,
            row,
            key="capture_refine_samples_path",
            label="样本文件路径",
            value=current.get("capture_refine_samples_path", "~/.local/share/recordian/refine-samples.jsonl"),
            hint="JSONL 文件；每行一条样本记录。",
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
        field_rows["sound_on_path"] = [sound_on_label, sound_on_box]
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
        field_rows["sound_off_path"] = [sound_off_label, sound_off_box]
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

        sec_wake_model = _create_collapsible_section(tab_wake, "模型与阈值（高级，默认无需修改）")
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

        sec_wake_advanced = _create_collapsible_section(tab_wake, "高级调优（仅在唤醒不稳定时调整）")
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

        status_label = Gtk.Label(label=SAVE_EFFECT_INTRO)
        status_label.set_xalign(0.0)
        status_label.set_line_wrap(True)
        status_label.set_max_width_chars(42)
        status_label.set_lines(2)
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
            if kind == "mapped":
                ids = mapped_ids.get(key, [])
                index = widget.get_active()
                if index < 0 or index >= len(ids):
                    return ""
                return ids[index]
            if kind == "combo":
                text = widget.get_active_text()
                return text if text is not None else ""
            if kind == "text":
                buffer = widget.get_buffer()
                return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
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

        def _set_form_value(key: str, value: object) -> None:
            target = entries.get(key)
            if not target:
                return
            kind, widget = target
            if kind == "bool":
                widget.set_active(bool(value))
                return
            if kind == "entry":
                widget.set_text(str(value))
                return
            if kind == "text":
                widget.get_buffer().set_text(str(value))
                return
            if kind == "mapped":
                token = str(value)
                ids = mapped_ids.setdefault(key, [])
                if token not in ids:
                    ids.append(token)
                    widget.append_text(token)
                widget.set_active(ids.index(token))
                return
            if kind == "combo":
                token = str(value)
                model = widget.get_model()
                for index, row_data in enumerate(model or []):
                    if str(row_data[0]) == token:
                        widget.set_active(index)
                        return
                widget.append_text(token)
                refreshed = widget.get_model()
                widget.set_active(len(list(refreshed)) - 1 if refreshed is not None else 0)

        def _set_rows_visible(keys: tuple[str, ...], visible: bool) -> None:
            for key in keys:
                for widget in field_rows.get(key, []):
                    widget.set_no_show_all(not visible)
                    widget.set_visible(bool(visible))

        def _set_rows_sensitive(keys: tuple[str, ...], sensitive: bool) -> None:
            for key in keys:
                for widget in field_rows.get(key, []):
                    widget.set_sensitive(bool(sensitive))

        refine_dependent = (
            "refine_provider",
            "refine_api_base",
            "refine_api_key",
            "refine_api_model",
            "refine_model",
            "refine_device",
            "refine_n_gpu_layers",
            "refine_max_tokens",
            "enable_thinking",
            "capture_refine_samples",
            "capture_refine_samples_path",
        )
        wake_dependent = tuple(
            key for key in entries
            if key.startswith("wake_") and key != "enable_voice_wake"
        )
        remote_dependent = (
            "remote_paste_host",
            "remote_paste_port",
            "remote_paste_timeout_s",
            "remote_paste_mode",
            "remote_paste_sync_wait_s",
            "remote_paste_follow_deskflow_active_screen",
            "deskflow_active_screen_path",
            "deskflow_log_path",
            "remote_paste_screen_name",
        )
        preset_editor_widgets = [
            preset_combo,
            btn_set_current,
            preset_name_entry,
            btn_create,
            preset_text,
            btn_save_preset,
            btn_delete_preset,
            btn_refresh_preset,
        ]

        def _sync_provider_visibility(*_args: object) -> None:
            provider_id = str(_get_value("asr_provider")).strip()
            _set_rows_visible(("qwen_model",), provider_id in {"qwen-asr", "http-cloud"})
            _set_rows_visible(("qwen_max_new_tokens", "device"), provider_id == "qwen-asr")
            _set_rows_visible(("asr_endpoint",), provider_id == "http-cloud")
            _set_rows_visible(("asr_timeout_s",), provider_id in {"http-cloud", "confucius-asr"})
            _set_rows_visible(
                ("asr_realtime_endpoint", "asr_api_key"),
                provider_id in {"http-cloud", "confucius-asr"},
            )
            hints = endpoint_hints_for_provider(provider_id)
            for key, text in hints.items():
                hint = hint_labels.get(key)
                if hint is not None:
                    hint.set_text(text)

        def _sync_feature_sensitivity(*_args: object) -> None:
            refine_on = bool(_get_value("enable_text_refine"))
            _set_rows_sensitive(refine_dependent, refine_on)
            for widget in preset_editor_widgets:
                widget.set_sensitive(refine_on)
            if refine_on:
                _load_selected_preset()
            _set_rows_sensitive(wake_dependent, bool(_get_value("enable_voice_wake")))
            btn_record_owner_sample.set_sensitive(bool(_get_value("enable_voice_wake")))
            _set_rows_sensitive(remote_dependent, bool(_get_value("enable_remote_paste")))
            correction_on = bool(_get_value("enable_semif_correction"))
            provider_id = normalize_correction_provider(_get_value("correction_provider"))
            semif_selected = provider_id == "semif"
            _set_rows_sensitive(
                ("correction_provider", "contextual_aliases"),
                correction_on,
            )
            _set_rows_visible(("semif_endpoint", "semif_timeout_s"), semif_selected)
            _set_rows_visible(("jev_timeout_s",), not semif_selected)
            _set_rows_sensitive(("semif_endpoint", "semif_timeout_s"), correction_on and semif_selected)
            _set_rows_sensitive(("jev_timeout_s",), correction_on and not semif_selected)

        def _reconcile_confucius_endpoint(*, announce: bool) -> None:
            if str(_get_value("asr_provider")).strip() != "confucius-asr":
                return
            raw = str(_get_value("asr_realtime_endpoint")).strip()
            migrated = migrate_confucius_realtime_endpoint(raw)
            if migrated is not None and migrated != raw:
                _set_entry_text("asr_realtime_endpoint", migrated)
                if announce:
                    _set_status(CONFUCIUS_STOCK_MIGRATION_NOTICE)
                return
            problem = confucius_endpoint_problem(raw)
            if problem and announce:
                _set_status(problem)

        def _on_provider_changed(*_args: object) -> None:
            _sync_provider_visibility()
            _reconcile_confucius_endpoint(announce=True)

        hidden_overrides: dict[str, object] = {}

        def _apply_recommended(*_args: object) -> None:
            values = recommended_profile_values()
            provider_id = values.pop("asr_provider")
            endpoint = values.pop("asr_realtime_endpoint")
            hidden_overrides["enable_streaming_refine"] = values.pop("enable_streaming_refine", False)
            for key, value in values.items():
                _set_form_value(key, value)
            _set_form_value("asr_realtime_endpoint", str(endpoint))
            _set_form_value("asr_provider", provider_id)
            _sync_provider_visibility()
            _sync_feature_sensitivity()
            _set_status(RECOMMENDED_PROFILE_NOTICE)

        recommend_button.connect("clicked", _apply_recommended)
        entries["asr_provider"][1].connect("changed", _on_provider_changed)
        for master_key in (
            "enable_text_refine",
            "enable_voice_wake",
            "enable_remote_paste",
            "enable_semif_correction",
        ):
            entries[master_key][1].connect("notify::active", _sync_feature_sensitivity)
        entries["correction_provider"][1].connect("changed", _sync_feature_sensitivity)

        btn_record_owner_sample.connect("clicked", lambda *_: app.open_speaker_enrollment_wizard())

        def _save(*, restart_backend: bool) -> None:
            latest_config: dict[str, object] = {}
            changed_keys: list[str] = []
            migrated_endpoint = False

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
                provider_id = str(_get_value("asr_provider")).strip()
                realtime_endpoint = str(_get_value("asr_realtime_endpoint")).strip()
                http_endpoint = str(_get_value("asr_endpoint")).strip()
                if provider_id == "confucius-asr":
                    migrated = migrate_confucius_realtime_endpoint(realtime_endpoint)
                    if migrated is not None and migrated != realtime_endpoint:
                        migrated_endpoint = True
                        _set_entry_text("asr_realtime_endpoint", migrated)
                    elif confucius_endpoint_problem(realtime_endpoint):
                        status_label.set_text(confucius_endpoint_problem(realtime_endpoint) or "")
                        return
                elif provider_id == "http-cloud":
                    problem = http_cloud_endpoint_problem(realtime_endpoint, http_endpoint)
                    if problem:
                        status_label.set_text(problem)
                        return
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
                    "enable_semif_correction": bool(_get_value("enable_semif_correction")),
                    "correction_provider": normalize_correction_provider(_get_value("correction_provider")),
                    "semif_endpoint": str(_get_value("semif_endpoint")).strip(),
                    "semif_timeout_s": normalize_semif_timeout_s(
                        _parse_float_field("semif_timeout_s", float(current.get("semif_timeout_s", DEFAULT_SEMIF_TIMEOUT_S)))
                    ),
                    "jev_timeout_s": normalize_jev_timeout_s(
                        _parse_float_field("jev_timeout_s", float(current.get("jev_timeout_s", DEFAULT_JEV_TIMEOUT_S)))
                    ),
                    "contextual_aliases": normalize_contextual_aliases(str(_get_value("contextual_aliases"))),
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
                    "hotword_replacement": latest_config.get("hotword_replacement", []),
                    "enable_streaming_refine": hidden_overrides.get(
                        "enable_streaming_refine",
                        latest_config.get("enable_streaming_refine", False),
                    ),
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
                changed_keys = [key for key, value in payload.items() if latest_config.get(key) != value]
                effect = combined_setting_effect(changed_keys) if changed_keys else SettingEffect.IMMEDIATE
                busy = str(getattr(getattr(app, "state", None), "status", "")) in DICTATION_BUSY_STATUSES
                if busy and restart_backend and effect is SettingEffect.RESTART_REQUIRED:
                    status_label.set_text(BUSY_SAVE_MESSAGE)
                    return
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
            changed_labels = "、".join(KEY_LABEL_MAP.get(k, k) for k in changed_keys)
            if restarted:
                message = "已保存并生效。听写服务已重新启动。"
            elif effect is SettingEffect.IMMEDIATE:
                message = "已保存并生效。这次修改马上起作用。"
            elif effect is SettingEffect.NEXT_SESSION:
                message = "已保存并生效。下次说话时使用新设置。"
            else:
                message = effect_status_message(effect, restarted=restarted)
            if migrated_endpoint:
                message = "已把旧的默认识别地址换成本机 Confucius 地址。" + message
            if changed_labels:
                message = f"{message} 变更：{changed_labels}。"
            status_label.set_text(message)
            app._update_tray_menu()

        btn_save_restart = Gtk.Button(label="保存并生效")
        btn_save_restart.get_style_context().add_class("suggested-action")
        btn_save_restart.connect("clicked", lambda *_: _save(restart_backend=True))
        footer.pack_end(btn_save_restart, False, False, 0)

        btn_cancel = Gtk.Button(label="取消")
        btn_cancel.connect("clicked", lambda *_: win.destroy())
        footer.pack_end(btn_cancel, False, False, 0)

        def _on_destroy(*_args: object) -> None:
            app._gtk_settings_window = None

        win.connect("destroy", _on_destroy)
        advanced.connect("notify::expanded", _sync_advanced_space)
        _sync_provider_visibility()
        _sync_feature_sensitivity()
        win.set_default_size(780, 680)
        win.resize(780, 680)
        win.show_all()
        _sync_advanced_space()
        _reconcile_confucius_endpoint(announce=True)
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
