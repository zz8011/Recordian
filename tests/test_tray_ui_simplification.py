"""Behavior tests for the simplified tray menu and settings form.

GTK cases build real widgets against a temporary config. They must not
construct TrayApp or BackendManager: that manager's cleanup can stop the
live hotkey process.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from recordian.config import ConfigManager
from recordian.recommended_profile import (
    CONFUCIUS_LOCAL_WS_ENDPOINT,
    CREDENTIAL_KEYS,
    LANGUAGE_CHOICES,
    PROVIDER_CHOICES,
    RECOMMENDED_PROFILE_NOTICE,
    TRIGGER_MODE_CHOICES,
    choice_id,
    choice_label,
    confucius_endpoint_problem,
    http_cloud_endpoint_problem,
    merge_recommended_profile,
    migrate_confucius_realtime_endpoint,
    recommended_profile_values,
    status_headline,
)
from recordian.setting_effects import SettingEffect
from recordian.tray_app import RecentRunObservation, UiState

SECRET = "kept-credential"
REFINE_SECRET = "kept-refine-credential"


def test_recommended_profile_preserves_secrets_hotwords_and_unknown_fields() -> None:
    current = {
        "asr_api_key": SECRET,
        "refine_api_key": REFINE_SECRET,
        "remote_paste_key": "kept-remote",
        "remote_code": "kept-remote-code",
        "asr_context": "微信, Recordian",
        "hotword": ["自定义热词"],
        "hotword_replacement": ["错词→正词"],
        "semif_endpoint": "http://10.2.2.2:9/semif",
        "input_device": "headset",
        "custom_unknown_flag": {"keep": True},
        "asr_realtime_endpoint": "http://127.0.0.1:8000",
        "auto_hard_enter": True,
        "enable_text_refine": True,
        "enable_streaming_refine": True,
        "enable_semif_correction": True,
        "correction_provider": "jev",
        "jev_timeout_s": 1.5,
        "contextual_aliases": [{"heard": "jeff", "word": "jev", "meaning": "软件工具"}],
    }
    merged = merge_recommended_profile(current)
    profile = recommended_profile_values()

    assert CREDENTIAL_KEYS.isdisjoint(profile)
    assert merged["asr_api_key"] == SECRET
    assert merged["refine_api_key"] == REFINE_SECRET
    assert merged["remote_paste_key"] == "kept-remote"
    assert merged["remote_code"] == "kept-remote-code"
    assert merged["asr_context"] == "微信, Recordian"
    assert merged["hotword"] == ["自定义热词"]
    assert merged["hotword_replacement"] == ["错词→正词"]
    assert merged["semif_endpoint"] == "http://10.2.2.2:9/semif"
    assert merged["input_device"] == "headset"
    assert merged["custom_unknown_flag"] == {"keep": True}
    assert merged["asr_provider"] == "confucius-asr"
    assert merged["asr_realtime_endpoint"] == CONFUCIUS_LOCAL_WS_ENDPOINT
    assert merged["qwen_language"] == "auto"
    assert merged["enable_streaming_commit"] is True
    assert merged["auto_hard_enter"] is False
    assert merged["enable_text_refine"] is False
    assert merged["enable_voice_wake"] is False
    assert merged["enable_remote_paste"] is False
    # A user's configured SemIf and contextual aliases survive the profile;
    # the profile itself stays silent so fresh generic defaults remain off.
    assert "enable_semif_correction" not in profile
    assert merged["enable_semif_correction"] is True
    assert merged["contextual_aliases"] == [{"heard": "jeff", "word": "jev", "meaning": "软件工具"}]
    assert "correction_provider" not in profile
    assert "jev_timeout_s" not in profile
    assert merged["correction_provider"] == "jev"
    assert merged["jev_timeout_s"] == 1.5
    assert "enable_semif_correction" not in merge_recommended_profile({})
    assert merge_recommended_profile({}).get("enable_semif_correction", False) is False
    assert merged["trigger_mode"] == "ptt"
    assert merged["hotkey"] == "<ctrl_r>"
    assert merged["toggle_hotkey"] == "<alt_r>"
    assert merged["sample_rate"] == 16000
    assert merged["channels"] == 1
    assert merged["record_format"] == "wav"
    assert merged["record_backend"] == "auto"
    assert merged["commit_backend"] == "fcitx"
    assert merged["enable_streaming_refine"] is False
    assert current["custom_unknown_flag"] == {"keep": True}
    assert "口令" in RECOMMENDED_PROFILE_NOTICE
    assert "还没保存" in RECOMMENDED_PROFILE_NOTICE
    assert "ws://" not in RECOMMENDED_PROFILE_NOTICE
    assert "http://" not in RECOMMENDED_PROFILE_NOTICE


@pytest.mark.parametrize("choices", [TRIGGER_MODE_CHOICES, LANGUAGE_CHOICES, PROVIDER_CHOICES])
def test_choice_ids_round_trip(choices: tuple[tuple[str, str], ...]) -> None:
    for item_id, _label in choices:
        assert choice_id(choices, choice_label(choices, item_id)) == item_id
    assert choice_id(choices, "legacy-custom") == "legacy-custom"
    assert choice_label(choices, "legacy-custom") == "legacy-custom"


@pytest.mark.parametrize(
    ("raw", "migrated"),
    [
        ("", CONFUCIUS_LOCAL_WS_ENDPOINT),
        ("http://127.0.0.1:8000", CONFUCIUS_LOCAL_WS_ENDPOINT),
        ("http://127.0.0.1:8000/", CONFUCIUS_LOCAL_WS_ENDPOINT),
        ("HTTP://127.0.0.1:8000/v1/realtime", CONFUCIUS_LOCAL_WS_ENDPOINT),
        ("http://127.0.0.1:8000/v1/audio/transcriptions", CONFUCIUS_LOCAL_WS_ENDPOINT),
    ],
)
def test_stock_stale_realtime_endpoint_migrates(raw: str, migrated: str) -> None:
    assert migrate_confucius_realtime_endpoint(raw) == migrated


@pytest.mark.parametrize(
    "raw",
    [
        "ws://10.1.2.3:9000/asr_stream_api_v1",
        "wss://speech.example/asr_stream_api_v1",
        "ws://127.0.0.1:8321",
        "http://10.9.8.7:9999/custom",
        "http://127.0.0.1:8001",
        "http://192.168.5.111:40002",
    ],
)
def test_custom_realtime_endpoints_are_not_rewritten(raw: str) -> None:
    assert migrate_confucius_realtime_endpoint(raw) is None


def test_confucius_endpoint_rules_accept_custom_paths_and_reject_bad_hosts() -> None:
    assert confucius_endpoint_problem("ws://10.1.2.3:9000/asr_stream_api_v1") is None
    assert confucius_endpoint_problem("wss://speech.example/proxy/stream?x=1") is None
    assert confucius_endpoint_problem("ws://127.0.0.1:8321") is None
    assert confucius_endpoint_problem("ws://[::1]:8321/custom") is None
    assert confucius_endpoint_problem("ws://127.0.0.1:8321/v1/realtime") is None
    custom = confucius_endpoint_problem("http://10.9.8.7:9999/custom")
    assert custom is not None
    assert "不会被自动改掉" in custom
    assert "10.9.8.7" not in custom
    bad_port = confucius_endpoint_problem("ws://127.0.0.1:99999/custom")
    assert bad_port is not None
    assert "端口" in bad_port
    bad_v6 = confucius_endpoint_problem("ws://[::1:8321/custom")
    assert bad_v6 is not None
    cloud = http_cloud_endpoint_problem("ws://127.0.0.1:8321/asr_stream_api_v1", "")
    assert cloud is not None
    assert http_cloud_endpoint_problem("http://192.168.5.111:40002", "https://example/v1/audio/transcriptions") is None
    missing_host = http_cloud_endpoint_problem("", "http://:80/v1")
    assert missing_host is not None
    assert "主机" in missing_host
    bad_http_port = http_cloud_endpoint_problem("http://192.168.5.111:70000", "")
    assert bad_http_port is not None
    assert "端口" in bad_http_port


def test_status_headline_uses_app_state_and_hotkey() -> None:
    ready = UiState(status="idle", backend_running=True, detail="Ready")
    assert status_headline(ready, {"trigger_mode": "ptt", "hotkey": "<ctrl_r>"}) == "就绪 · 按住 右 Ctrl 说话"
    assert status_headline(UiState(status="recording", backend_running=True)) == "正在听写"
    assert status_headline(UiState(status="processing", backend_running=True)) == "正在识别"
    assert status_headline(UiState(status="warming", backend_running=True)) == "正在准备"
    assert status_headline(UiState(status="error", backend_running=True, detail="boom")) == "出错"
    stopped = status_headline(
        UiState(status="stopped", backend_running=False),
        {"trigger_mode": "toggle", "hotkey": "<alt_r>"},
    )
    assert stopped == "已暂停 · 按 右 Alt 开始或停止"
    observed = UiState(
        status="idle",
        backend_running=True,
        last_run=RecentRunObservation(text="你好", asr_path="prefetched", detected_language="zh"),
    )
    headline = status_headline(observed, {"trigger_mode": "ptt", "hotkey": "<ctrl_r>"})
    assert "你好" not in headline
    assert "prefetched" not in headline


def _gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    checked = Gtk.init_check([])
    ready = checked[0] if isinstance(checked, tuple) else bool(checked)
    if not ready:
        pytest.skip("GTK display is not available")
    return Gtk, GLib


def _pump(glib: Any, rounds: int = 40) -> None:
    context = glib.MainContext.default()
    for _ in range(rounds):
        while context.pending():
            context.iteration(False)


def _settle(glib: Any, milliseconds: int = 100) -> None:
    import time

    context = glib.MainContext.default()
    deadline = time.monotonic() + (milliseconds / 1000)
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def _window_rect(widget: Any, window: Any) -> tuple[int, int, int, int]:
    origin = widget.translate_coordinates(window, 0, 0)
    assert origin is not None
    alloc = widget.get_allocation()
    return int(origin[0]), int(origin[1]), int(alloc.width), int(alloc.height)


def _walk(widget: Any):
    yield widget
    children = []
    if hasattr(widget, "get_children"):
        try:
            children = list(widget.get_children())
        except Exception:
            children = []
    elif hasattr(widget, "get_child"):
        child = widget.get_child()
        if child is not None:
            children = [child]
    for child in children:
        yield from _walk(child)


def _labels(root: Any, gtk: Any) -> list[Any]:
    return [widget for widget in _walk(root) if isinstance(widget, gtk.Label)]


def _button(root: Any, gtk: Any, label: str) -> Any:
    for widget in _walk(root):
        if isinstance(widget, gtk.Button) and widget.get_label() == label:
            return widget
    raise AssertionError(f"missing button {label}")


def _grid_sibling(root: Any, gtk: Any, label_text: str, kind: type) -> Any:
    for label in _labels(root, gtk):
        if label.get_text() != label_text:
            continue
        grid = label.get_parent()
        if not isinstance(grid, gtk.Grid):
            continue
        top = grid.child_get_property(label, "top-attach")
        for child in grid.get_children():
            if grid.child_get_property(child, "top-attach") != top:
                continue
            if isinstance(child, kind):
                return child
            for descendant in _walk(child):
                if isinstance(descendant, kind):
                    return descendant
    raise AssertionError(f"missing {kind.__name__} beside {label_text}")


def _switch_for(root: Any, gtk: Any, label_text: str) -> Any:
    for label in _labels(root, gtk):
        if label.get_text() != label_text:
            continue
        parent = label.get_parent()
        row = parent.get_parent() if parent is not None else None
        if row is None:
            continue
        for descendant in _walk(row):
            if isinstance(descendant, gtk.Switch):
                return descendant
    raise AssertionError(f"missing switch {label_text}")


def _menu_labels(menu: Any) -> list[str]:
    labels = []
    for child in menu.get_children():
        getter = getattr(child, "get_label", None)
        if getter is not None and getter():
            labels.append(getter())
    return labels


class _Root:
    def after(self, _delay: int, callback=None):
        if callback is not None:
            callback()
        return "after-id"


class _Backend:
    def __init__(self) -> None:
        self.restart_calls = 0
        self.start_calls = 0
        self.stop_calls = 0

    def restart(self) -> None:
        self.restart_calls += 1

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class _Indicator:
    def __init__(self) -> None:
        self.icon = ""

    def set_icon(self, path: str) -> None:
        self.icon = path


def _fake_app(tmp_path: Path, config: dict[str, Any]) -> Any:
    path = tmp_path / "hotkey.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    app = type("FakeApp", (), {})()
    app.config_path = path
    app.state = UiState(status="idle", backend_running=True, detail="Ready")
    app.root = _Root()
    app.backend = _Backend()
    app.calls: list[Any] = []
    app._config = dict(config)
    app._get_cached_config = lambda: app._config
    app._invalidate_config_cache = lambda: None
    app._update_tray_menu = lambda: None
    app._gtk_settings_window = None
    app._menu_syncing = False
    app._menu_sync_depth = 0
    app._preset_menu_last_sync_ts = 0.0
    app._appindicator_png_cache = {}
    app.indicator = _Indicator()
    app.open_context_editor = lambda: app.calls.append("context")
    app.open_settings = lambda: app.calls.append("settings")
    app.copy_last_text = lambda: app.calls.append("copy")
    app.open_speaker_enrollment_wizard = lambda: app.calls.append("speaker")
    app.open_diagnostics = lambda: app.calls.append("diagnostics")
    app.quit = lambda: app.calls.append("quit")
    app.toggle_streaming_commit = lambda enabled: app.calls.append(("streaming", bool(enabled)))
    app.toggle_text_refine = lambda enabled: app.calls.append(("refine", bool(enabled)))
    app.toggle_voice_wake = lambda enabled: app.calls.append(("wake", bool(enabled)))
    app.switch_preset = lambda name: app.calls.append(("preset", name))
    return app


def test_menu_refresh_does_not_mutate_or_restart(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    from recordian.tray_menu import build_appindicator_menu, update_tray_menu

    config = {
        "trigger_mode": "ptt",
        "hotkey": "<ctrl_r>",
        "enable_streaming_commit": False,
        "enable_text_refine": True,
        "enable_voice_wake": False,
        "refine_preset": "default",
    }
    app = _fake_app(tmp_path, config)
    app._gtk = gtk
    app._glib = glib
    menu = build_appindicator_menu(app, None, gtk, glib)
    labels = _menu_labels(menu)
    assert "开始听写" not in labels
    assert any(label == "暂停听写" for label in labels)
    assert "边说边出字" in labels
    assert "常用词..." in labels
    assert "设置..." in labels
    assert "复制上次文字" in labels
    assert "文字润色" not in labels
    assert "语音唤醒" not in labels
    assert app._appindicator_status_item.get_label() == "就绪 · 按住 右 Ctrl 说话"

    more = next(child for child in menu.get_children() if getattr(child, "get_label", lambda: "")() == "更多")
    more_labels = _menu_labels(more.get_submenu())
    assert "文字润色" in more_labels
    assert "语音唤醒" in more_labels
    assert "润色风格" in more_labels
    assert app._appindicator_preset_menu_item.get_sensitive()

    app._config["enable_text_refine"] = False
    app._config["enable_streaming_commit"] = True
    app._config["enable_voice_wake"] = True
    stored = json.loads(app.config_path.read_text(encoding="utf-8"))
    stored.update(
        {
            "enable_text_refine": False,
            "enable_streaming_commit": True,
            "enable_voice_wake": True,
            "refine_preset": "formal",
        }
    )
    app.config_path.write_text(json.dumps(stored), encoding="utf-8")
    app._preset_menu_last_sync_ts = 0.0
    app.calls.clear()
    update_tray_menu(app)
    _pump(glib)
    assert app.calls == []
    assert app.backend.restart_calls == 0
    assert app._appindicator_streaming_item.get_active()
    assert not app._appindicator_text_refine_item.get_active()
    assert app._appindicator_voice_wake_item.get_active()
    assert not app._appindicator_preset_menu_item.get_sensitive()

    app._config["enable_text_refine"] = True
    stored["enable_text_refine"] = True
    app.config_path.write_text(json.dumps(stored), encoding="utf-8")
    app._preset_menu_last_sync_ts = 0.0
    app.calls.clear()
    update_tray_menu(app)
    _pump(glib)
    assert app.calls == []
    assert app.backend.restart_calls == 0
    assert app._appindicator_preset_items["formal"].get_active()

    intent = app._appindicator_preset_items["intent"]
    intent.set_active(True)
    assert ("preset", "intent") in app.calls


def _open_settings(app: Any, gtk: Any, glib: Any, current: dict[str, Any]) -> Any:
    from recordian.tray_settings import open_settings_gtk

    app._gtk = gtk
    app._glib = glib
    open_settings_gtk(
        app,
        current=current,
        current_record_backend=str(current.get("record_backend", "auto")),
        current_record_format=str(current.get("record_format", "ogg")),
        current_refine_provider=str(current.get("refine_provider", "local")),
        current_commit_backend=str(current.get("commit_backend", "auto")),
        current_enable_thinking=current.get("enable_thinking", False),
        current_notify_backend=str(current.get("notify_backend", "auto")),
    )
    _pump(glib, 60)
    window = app._gtk_settings_window
    assert window is not None
    return window


def _base_current(**overrides: Any) -> dict[str, Any]:
    current = {
        "asr_provider": "qwen-asr",
        "asr_realtime_endpoint": "",
        "asr_endpoint": "http://127.0.0.1:8000/v1/audio/transcriptions",
        "asr_api_key": SECRET,
        "refine_api_key": REFINE_SECRET,
        "asr_context": "微信,编辑器",
        "qwen_language": "Chinese",
        "trigger_mode": "ptt",
        "hotkey": "<ctrl_r>",
        "toggle_hotkey": "<alt_r>",
        "stop_hotkey": "",
        "auto_hard_enter": False,
        "enable_streaming_commit": False,
        "enable_text_refine": False,
        "enable_voice_wake": False,
        "enable_remote_paste": False,
        "enable_semif_correction": False,
        "input_device": "headset",
        "sample_rate": 16000,
        "channels": 1,
        "record_format": "ogg",
        "record_backend": "auto",
        "hotword": ["自定义热词"],
        "custom_unknown_flag": {"keep": True},
    }
    current.update(overrides)
    return current


def test_settings_correction_provider_alias_and_recommend(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    original = _base_current(
        enable_semif_correction=True,
        correction_provider="jev",
        semif_endpoint="http://10.2.2.2:9/semif",
        semif_timeout_s=0.3,
        jev_timeout_s=1.4,
        contextual_aliases=[{"heard": "jeff", "word": "jev", "meaning": "软件工具"}],
        asr_context="微信,编辑器",
        hotword=["自定义热词"],
    )
    app = _fake_app(tmp_path, original)
    window = _open_settings(app, gtk, glib, original)
    assert _switch_for(window, gtk, "上下文纠词").get_active()
    provider = _grid_sibling(window, gtk, "纠词来源", gtk.ComboBoxText)
    assert provider.get_active_text() == "官方Jev（沿用本机登录）"
    hint_text = " ".join(label.get_text() for label in _labels(window, gtk))
    assert "复用 jev 的登录，密钥由 jev 管理" in hint_text
    assert "无需填写地址" in hint_text
    assert "本机已登录" not in hint_text
    assert "目前能登录" not in hint_text
    assert provider.get_visible()
    endpoint = _grid_sibling(window, gtk, "SemIf 服务地址", gtk.Entry)
    assert endpoint.get_text() == "http://10.2.2.2:9/semif"
    assert endpoint.get_visible() is False
    jev_timeout = _grid_sibling(window, gtk, "Jev 等待 (秒)", gtk.Entry)
    assert jev_timeout.get_visible()
    assert jev_timeout.get_text() == "1.4"
    alias = _grid_sibling(window, gtk, "语境别名", gtk.TextView)
    buffer = alias.get_buffer()
    alias_text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    assert "jeff→jev::软件工具" in alias_text
    assert _grid_sibling(window, gtk, "常用词", gtk.Entry).get_text() == "微信,编辑器"

    provider.set_active(0)
    _pump(glib)
    assert provider.get_active_text() == "本地 SemIf（默认）"
    assert endpoint.get_visible() is True
    assert jev_timeout.get_visible() is False
    provider.set_active(1)
    _pump(glib)

    _button(window, gtk, "使用本机推荐配置").clicked()
    _pump(glib)
    assert _switch_for(window, gtk, "上下文纠词").get_active()
    assert provider.get_active_text() == "官方Jev（沿用本机登录）"
    assert endpoint.get_text() == "http://10.2.2.2:9/semif"
    alias_text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    assert "jeff→jev::软件工具" in alias_text
    assert _grid_sibling(window, gtk, "常用词", gtk.Entry).get_text() == "微信,编辑器"

    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    saved = ConfigManager.load(app.config_path)
    assert saved["correction_provider"] == "jev"
    assert saved["enable_semif_correction"] is True
    assert saved["semif_endpoint"] == "http://10.2.2.2:9/semif"
    assert saved["jev_timeout_s"] == 1.4
    assert saved["contextual_aliases"] == [{"heard": "jeff", "word": "jev", "meaning": "软件工具"}]
    assert saved["asr_context"] == "微信,编辑器"
    raw = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert raw["hotword"] == ["自定义热词"]
    window.destroy()
    _pump(glib)


def test_settings_provider_visibility_and_no_model_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gtk, glib = _gtk()
    import recordian.tray_settings as tray_settings

    calls: list[str] = []
    monkeypatch.setattr(tray_settings, "fetch_model_list", lambda *_args, **_kwargs: calls.append("fetch"))
    app = _fake_app(tmp_path, _base_current(asr_provider="confucius-asr", asr_realtime_endpoint="ws://10.1.2.3:9000/asr_stream_api_v1"))
    window = _open_settings(
        app,
        gtk,
        glib,
        _base_current(
            asr_provider="confucius-asr",
            asr_realtime_endpoint="ws://10.1.2.3:9000/asr_stream_api_v1",
        ),
    )
    assert calls == []
    assert not _grid_sibling(window, gtk, "计算设备", gtk.ComboBoxText).get_visible()
    assert not _grid_sibling(window, gtk, "最长输出", gtk.Entry).get_visible()
    realtime = _grid_sibling(window, gtk, "实时地址", gtk.Entry)
    assert realtime.get_visible()
    assert realtime.get_text() == "ws://10.1.2.3:9000/asr_stream_api_v1"
    timeout = _grid_sibling(window, gtk, "等待上限 (秒)", gtk.Entry)
    assert timeout.get_visible()
    assert _grid_sibling(window, gtk, "识别接口", gtk.Entry).get_visible() is False
    assert _grid_sibling(window, gtk, "开始音效路径", gtk.Entry).is_sensitive()
    assert _grid_sibling(window, gtk, "结束音效路径", gtk.Entry).is_sensitive()
    window.destroy()
    _pump(glib)


def test_stock_endpoint_migrates_in_form_and_save_keeps_other_values(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    original = _base_current(
        asr_provider="confucius-asr",
        asr_realtime_endpoint="http://127.0.0.1:8000",
        auto_hard_enter=True,
    )
    app = _fake_app(tmp_path, original)
    before = app.config_path.read_text(encoding="utf-8")
    window = _open_settings(app, gtk, glib, original)
    realtime = _grid_sibling(window, gtk, "实时地址", gtk.Entry)
    assert realtime.get_text() == CONFUCIUS_LOCAL_WS_ENDPOINT
    assert app.config_path.read_text(encoding="utf-8") == before
    status = next(label.get_text() for label in _labels(window, gtk) if "旧的默认" in label.get_text())
    assert "保存并生效" in status
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    saved = ConfigManager.load(app.config_path)
    raw = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["asr_realtime_endpoint"] == CONFUCIUS_LOCAL_WS_ENDPOINT
    assert saved["auto_hard_enter"] is True
    assert saved["asr_api_key"] == SECRET
    assert saved["asr_context"] == "微信,编辑器"
    assert raw["hotword"] == ["自定义热词"]
    assert raw["custom_unknown_flag"] == {"keep": True}
    assert saved["asr_api_key"] == SECRET
    assert app.backend.restart_calls == 1
    window.destroy()
    _pump(glib)


def test_custom_incompatible_endpoint_blocks_save(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    original = _base_current(
        asr_provider="confucius-asr",
        asr_realtime_endpoint="http://10.9.8.7:9999/custom",
    )
    app = _fake_app(tmp_path, original)
    before = app.config_path.read_text(encoding="utf-8")
    window = _open_settings(app, gtk, glib, original)
    assert _grid_sibling(window, gtk, "实时地址", gtk.Entry).get_text() == "http://10.9.8.7:9999/custom"
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert app.config_path.read_text(encoding="utf-8") == before
    assert app.backend.restart_calls == 0
    status = " ".join(label.get_text() for label in _labels(window, gtk))
    assert "不会被自动改掉" in status
    window.destroy()
    _pump(glib)


def test_recommend_stages_form_until_save_and_primary_save_uses_apply_semantics(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    original = _base_current(
        enable_text_refine=True,
        refine_model="/tmp/refine-model",
        enable_streaming_refine=True,
    )
    app = _fake_app(tmp_path, original)
    before = app.config_path.read_text(encoding="utf-8")
    window = _open_settings(app, gtk, glib, original)
    _button(window, gtk, "使用本机推荐配置").clicked()
    _pump(glib)
    assert app.config_path.read_text(encoding="utf-8") == before
    assert _grid_sibling(window, gtk, "实时地址", gtk.Entry).get_text() == CONFUCIUS_LOCAL_WS_ENDPOINT
    assert _grid_sibling(window, gtk, "访问口令", gtk.Entry).get_text() == SECRET
    assert _grid_sibling(window, gtk, "常用词", gtk.Entry).get_text() == "微信,编辑器"
    assert _grid_sibling(window, gtk, "麦克风", gtk.Entry).get_text() == "headset"
    assert _grid_sibling(window, gtk, "上屏方式", gtk.ComboBoxText).get_active_text() == "fcitx"
    language = _grid_sibling(window, gtk, "语言", gtk.ComboBoxText)
    assert language.get_active_text() == "自动（中英）"
    assert not _switch_for(window, gtk, "启用文本精炼").get_active()
    assert not _grid_sibling(window, gtk, "精炼模型路径", gtk.Entry).get_sensitive()
    assert _grid_sibling(window, gtk, "精炼模型路径", gtk.Entry).get_text() == "/tmp/refine-model"
    notices = [label.get_text() for label in _labels(window, gtk) if label.get_text() == RECOMMENDED_PROFILE_NOTICE]
    assert notices
    assert "ws://" not in notices[0]

    _button(window, gtk, "取消").clicked()
    _pump(glib)
    assert app.config_path.read_text(encoding="utf-8") == before
    assert app._gtk_settings_window is None

    window = _open_settings(app, gtk, glib, original)
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    baseline = app.backend.restart_calls
    saved_once = ConfigManager.load(app.config_path)
    assert saved_once["asr_api_key"] == SECRET
    enter = _switch_for(window, gtk, "说完自动回车")
    enter.set_active(not enter.get_active())
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert app.backend.restart_calls == baseline
    assert ConfigManager.load(app.config_path)["auto_hard_enter"] is True
    hotkey = _grid_sibling(window, gtk, "按住说话的键", gtk.Entry)
    hotkey.set_text("<alt_r>")
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert app.backend.restart_calls == baseline + 1
    assert ConfigManager.load(app.config_path)["hotkey"] == "<alt_r>"
    assert combined_effect_is_restart()
    _button(window, gtk, "使用本机推荐配置").clicked()
    _pump(glib)
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert ConfigManager.load(app.config_path)["enable_streaming_refine"] is False
    assert ConfigManager.load(app.config_path)["asr_api_key"] == SECRET
    window.destroy()
    _pump(glib)


def test_save_and_menu_do_not_restart_while_dictating(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    from recordian.tray_menu import build_appindicator_menu

    original = _base_current(hotkey="<ctrl_r>", enable_streaming_commit=False)
    app = _fake_app(tmp_path, original)
    window = _open_settings(app, gtk, glib, original)
    before = app.config_path.read_text(encoding="utf-8")
    app.state.status = "recording"
    _grid_sibling(window, gtk, "按住说话的键", gtk.Entry).set_text("<alt_l>")
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert app.config_path.read_text(encoding="utf-8") == before
    assert app.backend.restart_calls == 0
    assert any(label.get_text() == "请先结束当前听写，再保存设置" for label in _labels(window, gtk))

    app.state.status = "processing"
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert app.config_path.read_text(encoding="utf-8") == before
    assert app.backend.restart_calls == 0

    app.state.status = "busy"
    _button(window, gtk, "保存并生效").clicked()
    _pump(glib)
    assert app.config_path.read_text(encoding="utf-8") == before
    assert app.backend.restart_calls == 0
    assert any(label.get_text() == "请先结束当前听写，再保存设置" for label in _labels(window, gtk))

    app.state.status = "idle"
    window.destroy()
    _pump(glib)

    app._gtk = gtk
    app._glib = glib
    app.events = type("Events", (), {"items": [], "put": lambda self, item: self.items.append(item)})()
    menu = build_appindicator_menu(app, None, gtk, glib)
    try:
        app.state.status = "recording"
        app.calls.clear()
        app._appindicator_streaming_item.set_active(True)
        _pump(glib)
        assert app.calls == []
        assert app.backend.restart_calls == 0
        assert not app._appindicator_streaming_item.get_active()
        assert any("请先结束当前听写" in str(item.get("message", "")) for item in app.events.items)

        app.events.items.clear()
        app.state.status = "busy"
        app._appindicator_streaming_item.set_active(True)
        _pump(glib)
        assert app.calls == []
        assert app.backend.restart_calls == 0
        assert not app._appindicator_streaming_item.get_active()
        assert any("请先结束当前听写" in str(item.get("message", "")) for item in app.events.items)
    finally:
        menu.destroy()
        _pump(glib)


def combined_effect_is_restart() -> bool:
    from recordian.setting_effects import combined_setting_effect

    return combined_setting_effect(["hotkey"]) is SettingEffect.RESTART_REQUIRED


def test_settings_window_screenshot_uses_sanitized_config(tmp_path: Path) -> None:
    gtk, glib = _gtk()
    from gi.repository import Gdk

    app = _fake_app(tmp_path, _base_current())
    window = _open_settings(app, gtk, glib, _base_current())
    _settle(glib, 300)
    width, height = window.get_allocated_width(), window.get_allocated_height()
    assert width <= 780 and height <= 680
    hotkey = _grid_sibling(window, gtk, "按住说话的键", gtk.Entry)
    recommend = _button(window, gtk, "使用本机推荐配置")
    microphone = _grid_sibling(window, gtk, "麦克风", gtk.Entry)
    daily_scroll = hotkey.get_parent()
    while daily_scroll is not None and not isinstance(daily_scroll, gtk.ScrolledWindow):
        daily_scroll = daily_scroll.get_parent()
    assert daily_scroll is not None
    expander = next(
        widget for widget in _walk(window) if isinstance(widget, gtk.Expander) and widget.get_label() == "高级设置"
    )
    _daily_x, daily_y, _daily_w, daily_h = _window_rect(daily_scroll, window)
    _recommend_x, recommend_y, _recommend_w, _recommend_h = _window_rect(recommend, window)
    _hotkey_x, hotkey_y, _hotkey_w, _hotkey_h = _window_rect(hotkey, window)
    _mic_x, mic_y, _mic_w, mic_h = _window_rect(microphone, window)
    _expander_x, expander_y, _expander_w, expander_h = _window_rect(expander, window)
    assert daily_h >= int(height * 0.7)
    assert expander_h <= 56
    assert recommend_y < hotkey_y
    assert recommend_y < 180
    assert daily_y <= mic_y and mic_y + mic_h <= daily_y + daily_h
    assert expander_y >= daily_y + daily_h - 4
    visible_text = " ".join(label.get_text() for label in _labels(window, gtk))
    assert "内部写法" not in visible_text
    assert "微信窗口" not in visible_text

    import os

    raw_dir = os.environ.get("GROK_UI_SCREENSHOT_DIR", "")
    if not raw_dir:
        window.destroy()
        pytest.skip("screenshot directory not requested")
    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    status = next(label for label in _labels(window, gtk) if label.get_text().startswith("点「保存并生效」"))
    assert status.get_layout().get_line_count() <= 2
    daily = _capture_window(window, gtk, Gdk)
    daily_path = target_dir / "settings-daily.png"
    daily.savev(str(daily_path), "png", [], [])
    assert daily.get_width() <= 780
    assert daily.get_height() <= 680
    assert daily_path.stat().st_size > 1000

    expander.set_expanded(True)
    _settle(glib, 300)
    _advanced_x, advanced_y, _advanced_w, advanced_h = _window_rect(expander, window)
    _daily_x, _daily_y, _daily_w, opened_daily_h = _window_rect(daily_scroll, window)
    notebook = next(widget for widget in _walk(expander) if isinstance(widget, gtk.Notebook))
    _notebook_x, _notebook_y, _notebook_w, notebook_h = _window_rect(notebook, window)
    assert opened_daily_h >= 160
    assert advanced_h >= 200
    assert notebook_h >= 160
    assert opened_daily_h + advanced_h >= int(height * 0.7)
    advanced = _capture_window(window, gtk, Gdk)
    advanced_path = target_dir / "settings-advanced.png"
    advanced.savev(str(advanced_path), "png", [], [])
    assert advanced.get_width() < 1200
    del advanced_y
    window.destroy()
    _pump(glib)


def _capture_window(window: Any, gtk: Any, gdk: Any) -> Any:
    gdk_window = window.get_window()
    assert gdk_window is not None
    width = window.get_allocated_width()
    height = window.get_allocated_height()
    assert width > 200 and height > 200
    pixbuf = gdk.pixbuf_get_from_window(gdk_window, 0, 0, width, height)
    assert pixbuf is not None
    return pixbuf
