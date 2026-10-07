"""Synthetic refinement settings: explicit discovery, staged secrets and stale replies."""

import threading
import time
from types import SimpleNamespace

import pytest

from recordian.config import ConfigManager
from recordian.settings_draft import SettingsDraft


def test_refine_key_is_staged_cancelled_and_only_written_on_save(tmp_path):
    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"refine_api_key": "old-fixture", "asr_api_key": "untouched-fixture"})
    before = path.read_bytes()
    draft = SettingsDraft({}, {"auto_hard_enter": False})
    draft.set_refine_key("new-fixture")
    assert draft.dirty and path.read_bytes() == before
    draft.cancel()
    assert not draft.dirty and path.read_bytes() == before
    draft.set_refine_key("new-fixture")
    draft.restore()
    assert "refine_api_key" not in draft.changes()
    draft.set_refine_key("new-fixture")
    draft.persist(path, apply_now=False)
    saved = ConfigManager.load(path)
    assert saved["refine_api_key"] == "new-fixture" and saved["asr_api_key"] == "untouched-fixture"
    assert not draft.dirty and draft.refine_key == ""
    draft.set_refine_key("   ")
    assert not draft.dirty


@pytest.fixture
def ui(tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    if not Gtk.init_check()[0]:
        pytest.skip("GTK display required")
    from recordian.native_settings import NativeSettingsWindow

    path = tmp_path / "hotkey.json"
    current = {
        "refine_provider": "cloud",
        "refine_api_base": "http://example.invalid:8123/v1",
        "refine_api_model": "manual-fixture",
        "refine_api_key": "saved-fixture",
    }
    ConfigManager.save(path, current)
    app = SimpleNamespace(
        config_path=path,
        state=SimpleNamespace(status="idle"),
        _gtk_settings_window=None,
        root=SimpleNamespace(after=lambda *_: None),
        backend=SimpleNamespace(restart=lambda: None),
        _invalidate_config_cache=lambda: None,
        _update_tray_menu=lambda: None,
    )
    window = NativeSettingsWindow(app, current, page="refine")
    yield window
    window.window.destroy()


def pump_until(predicate):
    from gi.repository import GLib

    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    assert predicate()


def test_button_uses_draft_endpoint_and_key_without_auto_request_or_save(ui, monkeypatch):
    from recordian import native_settings

    calls = []
    gate = threading.Event()

    def fetch(base, key, **kwargs):
        calls.append((base, key, threading.get_ident()))
        gate.wait(2)
        return ["model-a", "model-b"]

    monkeypatch.setattr(native_settings, "fetch_model_list", fetch)
    before = ui.app.config_path.read_bytes()
    entry = ui.controls["refine_api_key"][1]
    assert entry.get_text() == "" and not entry.get_visibility()
    ui.controls["refine_api_base"][1].set_text("http://example.invalid:9345/v1")
    entry.set_text("new-fixture")
    assert not calls
    ui.discover_button.emit("clicked")
    pump_until(lambda: bool(calls))
    assert calls[0][:2] == ("http://example.invalid:9345/v1", "new-fixture")
    assert calls[0][2] != threading.get_ident() and not ui.discover_button.get_sensitive()
    gate.set()
    pump_until(lambda: ui.discover_button.get_sensitive())
    combo = ui.controls["refine_api_model"][1]
    assert combo.get_child().get_text() == "manual-fixture"
    combo.set_active(1)
    assert ui.draft.values["refine_api_model"] == "model-b"
    assert ui.app.config_path.read_bytes() == before
    assert ui.save_changes()
    saved = ConfigManager.load(ui.app.config_path)
    assert saved["refine_api_model"] == "model-b" and saved["refine_api_key"] == "new-fixture"
    assert entry.get_text() == "" and not ui.draft.dirty


def test_invalid_endpoint_empty_reply_and_saved_key_fallback(ui, monkeypatch):
    from recordian import native_settings

    calls = []
    monkeypatch.setattr(native_settings, "fetch_model_list", lambda base, key, **_: calls.append(key) or [])
    ui.controls["refine_api_base"][1].set_text("not-a-url")
    ui.discover_button.emit("clicked")
    assert not calls and ui.discover_button.get_sensitive()
    ui.cancel_changes()
    ui.discover_button.emit("clicked")
    pump_until(lambda: bool(calls) and ui.discover_button.get_sensitive())
    assert calls == ["saved-fixture"]
    assert ui.controls["refine_api_model"][1].get_child().get_text() == "manual-fixture"
    assert "手动" in ui.discovery_status.get_text()


@pytest.mark.parametrize("action", ["edit", "cancel", "destroy", "provider"])
def test_late_reply_cannot_apply_after_connection_edit_cancel_close_or_provider_change(ui, monkeypatch, action):
    from recordian import native_settings

    gate = threading.Event()
    finished = threading.Event()

    def fetch(*args, **kwargs):
        gate.wait(2)
        finished.set()
        return ["stale-fixture"]

    monkeypatch.setattr(native_settings, "fetch_model_list", fetch)
    ui.discover_button.emit("clicked")
    if action == "edit":
        ui.controls["refine_api_base"][1].set_text("http://other.invalid/v1")
    elif action == "cancel":
        ui.cancel_changes()
    elif action == "provider":
        ui.controls["refine_provider"][1].set_active_id("local")
    else:
        ui.window.destroy()
    gate.set()
    pump_until(finished.is_set)
    pump_until(lambda: not ui.discovery_running)
    combo = ui.controls["refine_api_model"][1]
    assert "stale-fixture" not in [row[0] for row in combo.get_model()]
