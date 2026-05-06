from pathlib import Path

from recordian.backend_manager import parse_backend_event_line
from recordian.config import ConfigManager
from recordian.tray_gui import (
    RecentRunObservation,
    UiState,
    _blend_hex,
    _collect_recent_runtime_rows,
    _derive_openai_models_endpoint,
    _extract_recent_run_observation,
    _format_recent_run_log_suffix,
    _export_auto_lexicon_db,
    _format_diagnostic_report,
    _hex_with_alpha,
    _import_auto_lexicon_db,
    _load_hotkey_default_config,
    _next_event_poll_delay_ms,
    _overlay_hide_delay_seconds,
    _parse_bool,
    _save_config_changes,
    _sqlite_backup,
    _status_summary_label,
    _truncate,
    collect_runtime_diagnostics,
)
from recordian.waveform_renderer import WaveformRenderer


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


def test_next_event_poll_delay_ms_backs_off_when_idle() -> None:
    assert _next_event_poll_delay_ms(handled_events=0) == 180
    assert _next_event_poll_delay_ms(handled_events=3) == 24


def test_status_summary_label_includes_last_language() -> None:
    state = UiState(last_run=RecentRunObservation(record_ms=121.0, transcribe_ms=200.0, detected_language="zh", asr_path="prefetched"))

    assert _status_summary_label(state) == "时间: 321 ms | 语言: zh | 路径: prefetched"


def test_extract_recent_run_observation_parses_result_payload() -> None:
    observation, commit_info = _extract_recent_run_observation(
        {
            "text": "hello",
            "detected_language": "en",
            "asr_provider": "http-cloud",
            "asr_path": "prefetched",
            "asr_capabilities": "realtime,context",
            "record_latency_ms": 100.0,
            "transcribe_latency_ms": 200.0,
            "refine_latency_ms": 50.0,
            "commit": {"backend": "xdotool", "committed": True, "detail": "typed"},
        }
    )

    assert observation.text == "hello"
    assert observation.detected_language == "en"
    assert observation.asr_provider == "http-cloud"
    assert observation.asr_path == "prefetched"
    assert observation.asr_capabilities == "realtime,context"
    assert observation.total_ms == 350.0
    assert commit_info == {"backend": "xdotool", "committed": True, "detail": "typed"}


def test_extract_recent_run_observation_gracefully_handles_non_dict_result() -> None:
    observation, commit_info = _extract_recent_run_observation("not-a-dict")

    assert observation == RecentRunObservation()
    assert commit_info == {}


def test_format_recent_run_log_suffix_omits_empty_fields() -> None:
    suffix = _format_recent_run_log_suffix(
        RecentRunObservation(detected_language="zh", asr_provider="http-cloud", asr_path="streaming_commit")
    )

    assert suffix == " lang=zh provider=http-cloud path=streaming_commit"


def test_collect_recent_runtime_rows_surfaces_last_observation() -> None:
    state = UiState(
        last_run=RecentRunObservation(
            record_ms=156.0,
            transcribe_ms=300.0,
            detected_language="en",
            asr_provider="http-cloud",
            asr_path="streaming_commit",
            asr_capabilities="realtime,hotwords,context",
            text="This is a recent transcript",
        ),
    )

    rows = _collect_recent_runtime_rows(state)

    assert [row["label"] for row in rows] == [
        "最近 ASR 路径",
        "最近 ASR 提供方",
        "最近识别语言",
        "最近 ASR 能力",
        "最近识别文本",
        "最近耗时",
    ]
    assert rows[0]["detail"] == "streaming_commit"
    assert rows[1]["detail"] == "http-cloud"
    assert rows[2]["detail"] == "en"
    assert rows[3]["detail"] == "realtime,hotwords,context"
    assert rows[4]["detail"] == "This is a recent transcript"
    assert rows[5]["detail"] == "456 ms"


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


def test_load_hotkey_default_config_matches_backend_defaults() -> None:
    defaults = _load_hotkey_default_config(include_sound_defaults=True)

    assert defaults["refine_model"] == "Qwen/Qwen3-0.6B"
    assert defaults["refine_api_base"] == "https://api.minimaxi.com/anthropic"
    assert defaults["refine_api_model"] == "claude-3-5-sonnet-20241022"
    assert defaults["enable_remote_paste"] is False
    assert defaults["remote_paste_port"] == 24872
    assert defaults["remote_paste_mode"] == "direct"
    assert defaults["remote_paste_sync_wait_s"] == 0.35
    assert defaults["remote_paste_follow_deskflow_active_screen"] is False
    assert defaults["deskflow_active_screen_path"] == str(Path("~/.local/state/deskflow/active_screen.json").expanduser())
    assert defaults["deskflow_log_path"] == ""
    assert defaults["remote_paste_screen_name"] == ""
    assert defaults["wake_auto_stop_silence_s"] == 1.5


def test_derive_openai_models_endpoint() -> None:
    assert (
        _derive_openai_models_endpoint("http://127.0.0.1:8000/v1/audio/transcriptions")
        == "http://127.0.0.1:8000/v1/models"
    )
    assert _derive_openai_models_endpoint("http://127.0.0.1:8000/asr") is None


def test_collect_runtime_diagnostics_reports_http_cloud_probe(tmp_path: Path) -> None:
    config_path = tmp_path / "hotkey.json"
    config_path.write_text("{}", encoding="utf-8")

    owner_profile = tmp_path / "owner_voice_profile.json"
    owner_profile.write_text("{}", encoding="utf-8")

    rows = collect_runtime_diagnostics(
        {
            "asr_provider": "http-cloud",
            "asr_endpoint": "http://127.0.0.1:8000/v1/audio/transcriptions",
            "qwen_model": "Qwen3-ASR-0.6B",
            "enable_voice_wake": True,
            "wake_owner_verify": True,
            "wake_owner_profile": str(owner_profile),
            "auto_lexicon_db": str(tmp_path / "auto_lexicon.db"),
        },
        config_path=config_path,
        backend_running=True,
        backend_pid=4321,
        fetch_json=lambda url: (200, {"data": [{"id": "Qwen3-ASR-0.6B"}]}),
    )

    assert {"label": "后端进程", "status": "ok", "detail": "运行中 (PID 4321)"} in rows
    assert {"label": "语音唤醒", "status": "ok", "detail": "已开启"} in rows
    assert {"label": "声纹校验", "status": "ok", "detail": "已开启"} in rows
    assert {"label": "ASR 能力", "status": "info", "detail": "hotwords, context, language_hint, file_streaming"} in rows
    assert {"label": "ASR 模型", "status": "ok", "detail": "Qwen3-ASR-0.6B (已匹配远端模型)"} in rows

    report = _format_diagnostic_report(rows)
    assert "[OK] 后端进程: 运行中 (PID 4321)" in report
    assert "[INFO] ASR 能力: hotwords, context, language_hint, file_streaming" in report
    assert "[OK] ASR 模型: Qwen3-ASR-0.6B (已匹配远端模型)" in report


def test_save_config_changes_restarts_only_for_restart_required_settings(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    ConfigManager.save(path, {"auto_hard_enter": False, "enable_voice_wake": False})

    restart_calls: list[str] = []
    effect, restarted, changed = _save_config_changes(
        path,
        {"auto_hard_enter": True},
        apply_now=True,
        restart_callback=lambda: restart_calls.append("restart"),
    )
    assert effect.value == "immediate"
    assert restarted is False
    assert changed == ["auto_hard_enter"]
    assert restart_calls == []

    effect, restarted, changed = _save_config_changes(
        path,
        {"enable_voice_wake": True},
        apply_now=True,
        restart_callback=lambda: restart_calls.append("restart"),
    )
    assert effect.value == "restart_required"
    assert restarted is True
    assert changed == ["enable_voice_wake"]
    assert restart_calls == ["restart"]
