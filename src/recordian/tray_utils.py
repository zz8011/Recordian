from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from recordian.waveform_renderer import WaveformRenderer


def overlay_hide_delay_seconds(overlay: WaveformRenderer, state: str, detail: str) -> float:
    if state == "processing":
        return float(getattr(overlay, "PROCESSING_HIDE_DELAY_S", 0.50))
    if state == "error":
        return float(getattr(overlay, "ERROR_HIDE_DELAY_S", 1.55))
    if state == "idle":
        if detail.strip():
            return float(getattr(overlay, "IDLE_HIDE_DELAY_WITH_DETAIL_S", 1.10))
        return float(getattr(overlay, "IDLE_HIDE_DELAY_EMPTY_S", 0.35))
    return 0.0


def next_event_poll_delay_ms(*, handled_events: int) -> int:
    """Back off tray polling when no backend events are pending."""
    if handled_events > 0:
        return 24
    return 180


def sqlite_backup(src_path: Path, dst_path: Path) -> None:
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


def export_auto_lexicon_db(db_path: Path, export_path: Path) -> None:
    sqlite_backup(db_path, export_path)


def import_auto_lexicon_db(import_path: Path, db_path: Path) -> None:
    sqlite_backup(import_path, db_path)


def truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


HOTKEY_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd", "menu")


def normalize_hotkey_token(raw: str) -> str:
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
    if key in {"ctrl_l", "ctrl_r"}:
        modifiers.discard("ctrl")
    elif key in {"alt_l", "alt_r", "alt_gr"}:
        modifiers.discard("alt")
    elif key in {"shift_l", "shift_r"}:
        modifiers.discard("shift")
    elif key in {"cmd_l", "cmd_r"}:
        modifiers.discard("cmd")

    parts: list[str] = [mod for mod in HOTKEY_MODIFIER_ORDER if mod in modifiers]
    if key and key not in parts:
        parts.append(key)
    if not parts:
        return ""
    return "+".join(f"<{part}>" for part in parts)


def build_gtk_hotkey_spec(event: object, gdk: Any) -> str:
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


def parse_bool(value: str, *, default: bool) -> bool:
    token = value.strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def hex_with_alpha(color: str, alpha: float) -> str:
    # tkinter does not support #RRGGBBAA, so we blend against the dark bg.
    return blend_hex("#0b0f1a", color, alpha)


def blend_hex(a: str, b: str, ratio: float) -> str:
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


def save_config_changes(
    config_path: Path,
    changes: dict[str, object],
    *,
    apply_now: bool,
    restart_callback: Callable[[], object] | None = None,
) -> tuple[Any, bool, list[str]]:
    """Save configuration changes and return the effect, restart status, and changed keys."""
    from recordian.config import ConfigManager
    from recordian.setting_effects import SettingEffect, combined_setting_effect

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


__all__ = [
    "overlay_hide_delay_seconds",
    "next_event_poll_delay_ms",
    "sqlite_backup",
    "export_auto_lexicon_db",
    "import_auto_lexicon_db",
    "truncate",
    "HOTKEY_MODIFIER_ORDER",
    "normalize_hotkey_token",
    "format_hotkey_spec",
    "build_gtk_hotkey_spec",
    "parse_bool",
    "hex_with_alpha",
    "blend_hex",
    "save_config_changes",
]
