import json
from pathlib import Path

from recordian.tray_gui import (
    _parse_bool,
    _blend_hex,
    _export_auto_lexicon_db,
    _hex_with_alpha,
    _import_auto_lexicon_db,
    _overlay_hide_delay_seconds,
    _sqlite_backup,
    _truncate,
)
from recordian.waveform_renderer import WaveformRenderer
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
    # ConfigManager 现在会自动迁移配置，添加 version 和 policy 字段
    assert loaded["hotkey"] == payload["hotkey"]
    assert loaded["duration"] == payload["duration"]
    assert "version" in loaded  # 自动添加版本号


def test_color_and_truncate_helpers() -> None:
    assert _truncate("hello", 10) == "hello"
    assert _truncate("abcdefghijk", 8) == "abcde..."
    assert _hex_with_alpha("#ffffff", 0.5) == "#85878c"
    assert _blend_hex("#000000", "#ffffff", 0.5) == "#7f7f7f"
    assert _parse_bool("true", default=False)
    assert not _parse_bool("0", default=True)
    assert _parse_bool("unknown", default=True)


def test_overlay_hide_delay_seconds_matches_renderer_constants() -> None:
    class _FakeOverlay:
        PROCESSING_HIDE_DELAY_S = WaveformRenderer.PROCESSING_HIDE_DELAY_S
        ERROR_HIDE_DELAY_S = WaveformRenderer.ERROR_HIDE_DELAY_S
        IDLE_HIDE_DELAY_WITH_DETAIL_S = WaveformRenderer.IDLE_HIDE_DELAY_WITH_DETAIL_S
        IDLE_HIDE_DELAY_EMPTY_S = WaveformRenderer.IDLE_HIDE_DELAY_EMPTY_S

    overlay = _FakeOverlay()
    assert _overlay_hide_delay_seconds(overlay, "processing", "x") == WaveformRenderer.PROCESSING_HIDE_DELAY_S
    assert _overlay_hide_delay_seconds(overlay, "error", "x") == WaveformRenderer.ERROR_HIDE_DELAY_S
    assert _overlay_hide_delay_seconds(overlay, "idle", "有文字") == WaveformRenderer.IDLE_HIDE_DELAY_WITH_DETAIL_S
    assert _overlay_hide_delay_seconds(overlay, "idle", "") == WaveformRenderer.IDLE_HIDE_DELAY_EMPTY_S


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


def test_sqlite_backup_roundtrip(tmp_path: Path) -> None:
    import sqlite3

    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"

    conn = sqlite3.connect(str(src))
    try:
        conn.execute("CREATE TABLE terms (id INTEGER PRIMARY KEY, term TEXT)")
        conn.execute("INSERT INTO terms(term) VALUES (?)", ("openclaw",))
        conn.commit()
    finally:
        conn.close()

    _sqlite_backup(src, dst)

    dst_conn = sqlite3.connect(str(dst))
    try:
        row = dst_conn.execute("SELECT term FROM terms").fetchone()
        assert row is not None
        assert row[0] == "openclaw"
    finally:
        dst_conn.close()


def test_export_and_import_auto_lexicon_db(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "auto.db"
    exported = tmp_path / "backup.db"
    imported_target = tmp_path / "imported.db"

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE lexicon_terms (term TEXT PRIMARY KEY, accept_count INTEGER)")
        conn.execute("INSERT INTO lexicon_terms(term, accept_count) VALUES (?, ?)", ("recordian", 3))
        conn.commit()
    finally:
        conn.close()

    _export_auto_lexicon_db(db, exported)
    _import_auto_lexicon_db(exported, imported_target)

    check = sqlite3.connect(str(imported_target))
    try:
        row = check.execute("SELECT term, accept_count FROM lexicon_terms").fetchone()
        assert row == ("recordian", 3)
    finally:
        check.close()
