import json
from pathlib import Path

from recordian.tray_gui import (
    _parse_bool,
    _blend_hex,
    _hex_with_alpha,
    _truncate,
)
from recordian.backend_manager import parse_backend_event_line
from recordian.config import ConfigManager


def test_parse_backend_event_line_json_only() -> None:
    assert parse_backend_event_line("not_json") is None
    assert parse_backend_event_line('{"k":1}') is None
    assert parse_backend_event_line('{"event":"ready","x":1}') == {"event": "ready", "x": 1}


def test_load_save_runtime_config(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    payload = {"hotkey": "<ctrl>+<alt>+v", "duration": 3}
    ConfigManager.save(path, payload)
    loaded = ConfigManager.load(path)
    assert loaded == payload


def test_color_and_truncate_helpers() -> None:
    assert _truncate("hello", 10) == "hello"
    assert _truncate("abcdefghijk", 8) == "abcde..."
    assert _hex_with_alpha("#ffffff", 0.5) == "#85878c"
    assert _blend_hex("#000000", "#ffffff", 0.5) == "#7f7f7f"
    assert _parse_bool("true", default=False)
    assert not _parse_bool("0", default=True)
    assert _parse_bool("unknown", default=True)


def test_tray_gui_no_mktemp() -> None:
    """tray_gui.py 不应使用不安全的 tempfile.mktemp()"""
    import inspect
    from recordian import tray_gui
    source = inspect.getsource(tray_gui)
    assert "mktemp(" not in source, "tray_gui.py 仍在使用不安全的 mktemp()"


def test_tray_app_no_legacy_quick_menu_debug_print() -> None:
    """TrayApp 不应残留旧 quick menu 调试语句"""
    import inspect
    from recordian.tray_gui import TrayApp
    source = inspect.getsource(TrayApp)
    assert "open_quick_menu called" not in source
    assert "Menu position:" not in source
    assert "Menu popup successful" not in source
