import os

import pytest

from recordian.config import ConfigManager
from recordian.exceptions import ConfigError


def test_atomic_save_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "hotkey.json"
    path.write_text('{"auto_hard_enter": false}')
    before = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ConfigError):
        ConfigManager.save(path, {"auto_hard_enter": True})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_save_keeps_config_and_backup_private(tmp_path):
    path = tmp_path / "hotkey.json"
    path.write_text("{}")
    path.chmod(0o644)
    ConfigManager.save(path, {"custom_unknown": {"keep": True}})
    assert path.stat().st_mode & 0o777 == 0o600
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.glob("*.backup.*"))


def test_draft_saves_only_edited_fields_and_preserves_concurrent_unknown_values(tmp_path):
    from recordian.settings_draft import SettingsDraft

    path = tmp_path / "hotkey.json"
    ConfigManager.save(
        path, {"auto_hard_enter": False, "enable_agent": True, "asr_api_key": "synthetic-secret", "custom": 1}
    )
    draft = SettingsDraft(
        {"auto_hard_enter": False, "enable_agent": True}, {"auto_hard_enter": False, "enable_agent": False}
    )
    draft.set("auto_hard_enter", True)
    current = ConfigManager.load(path)
    current["custom"] = 2
    ConfigManager.save(path, current)
    draft.persist(path, apply_now=False)
    saved = ConfigManager.load(path)
    assert saved["auto_hard_enter"] is True and saved["enable_agent"] is True
    assert saved["asr_api_key"] == "synthetic-secret" and saved["custom"] == 2


def test_draft_cancel_and_defaults_never_write_until_saved(tmp_path):
    from recordian.settings_draft import SettingsDraft

    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"auto_hard_enter": True})
    before = path.read_bytes()
    draft = SettingsDraft({"auto_hard_enter": True}, {"auto_hard_enter": False})
    draft.restore()
    assert draft.dirty
    assert path.read_bytes() == before
    draft.cancel()
    assert draft.values["auto_hard_enter"] is True
    assert not draft.dirty and path.read_bytes() == before


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "-1", "0"])
def test_illegal_numeric_value_cannot_replace_config(tmp_path, value):
    from recordian.settings_draft import SettingsDraft

    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"sample_rate": 16000})
    draft = SettingsDraft({"sample_rate": "16000"}, {"sample_rate": "16000"})
    draft.set("sample_rate", value)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        draft.persist(path, apply_now=False)
    assert path.read_bytes() == before


def test_credentials_cannot_be_edited_through_native_draft():
    from recordian.settings_draft import SettingsDraft

    draft = SettingsDraft({"auto_hard_enter": False}, {"auto_hard_enter": False})
    with pytest.raises(ValueError):
        draft.set("asr_api_key", "new")


def test_busy_blocks_restart_required_draft_before_write(tmp_path):
    from recordian.settings_draft import SettingsDraft

    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"sample_rate": 16000})
    draft = SettingsDraft({"sample_rate": "16000"}, {"sample_rate": "16000"})
    draft.set("sample_rate", "24000")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        draft.persist(path, apply_now=True, status="recording")
    assert path.read_bytes() == before


def test_native_window_cancel_save_and_reopen(tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    if not Gtk.init_check()[0]:
        pytest.skip("GTK display required")
    from types import SimpleNamespace

    from recordian.native_settings import NativeSettingsWindow, open_settings_gtk

    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"auto_hard_enter": False, "enable_agent": True})
    app = SimpleNamespace(
        config_path=path,
        state=SimpleNamespace(status="idle"),
        _gtk_settings_window=None,
        _glib=GLib,
        root=SimpleNamespace(after=lambda delay, fn: None),
        backend=SimpleNamespace(restart=lambda: None),
        _invalidate_config_cache=lambda: None,
        _update_tray_menu=lambda: None,
    )
    ui = NativeSettingsWindow(app, {"auto_hard_enter": False, "enable_agent": True})
    ui.controls["enable_agent"][1].set_active(False)
    assert ConfigManager.load(path)["enable_agent"] is True
    ui.cancel_changes()
    assert ui.controls["enable_agent"][1].get_active() is True
    ui.controls["auto_hard_enter"][1].set_active(True)
    assert ui.save_changes()
    assert ConfigManager.load(path)["auto_hard_enter"] is True
    ui.window.destroy()
    app._gtk_settings_window = None
    open_settings_gtk(app, current=ConfigManager.load(path))
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)
    first = app._gtk_settings_window
    open_settings_gtk(app, current=ConfigManager.load(path))
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)
    assert app._gtk_settings_window is first
    assert app._native_settings.controls["auto_hard_enter"][1].get_active() is True
    first.destroy()


def test_cloud_asr_invalid_realtime_address_identifies_editable_field():
    from recordian.settings_draft import SettingsDraft

    current = {
        "asr_provider": "http-cloud",
        "asr_realtime_endpoint": "ws://127.0.0.1:8321",
        "asr_endpoint": "https://example.invalid/asr",
    }
    draft = SettingsDraft(current, current)
    assert "asr_realtime_endpoint" in draft.errors()


def test_enabled_cloud_refinement_requires_its_own_model_identifier(tmp_path):
    from recordian.settings_draft import SettingsDraft

    current = {
        "enable_text_refine": True,
        "refine_provider": "cloud",
        "refine_model": "local-model",
        "refine_api_model": "",
        "refine_api_base": "https://example.invalid/v1",
    }
    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, current)
    before = path.read_bytes()
    draft = SettingsDraft(current, current)
    draft.set("refine_model", "another-local-model")
    with pytest.raises(ValueError):
        draft.persist(path, apply_now=False)
    assert path.read_bytes() == before


def test_nonserializable_values_preserve_original_and_report_config_error(tmp_path):
    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"auto_hard_enter": False})
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        ConfigManager.save(path, {"custom": float("nan")})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_native_navigation_provider_validation_close_and_restore(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    if not Gtk.init_check()[0]:
        pytest.skip("GTK display required")
    from types import SimpleNamespace

    from recordian.native_settings import NativeSettingsWindow

    path = tmp_path / "hotkey.json"
    ConfigManager.save(
        path,
        {
            "asr_provider": "confucius-asr",
            "asr_realtime_endpoint": "ws://localhost:8321",
            "auto_hard_enter": True,
            "asr_api_key": "synthetic-secret",
        },
    )
    before = path.read_bytes()
    app = SimpleNamespace(
        config_path=path,
        state=SimpleNamespace(status="idle"),
        _gtk_settings_window=None,
        root=SimpleNamespace(after=lambda *args: None),
        backend=SimpleNamespace(restart=lambda: None),
        _invalidate_config_cache=lambda: None,
        _update_tray_menu=lambda: None,
    )
    ui = NativeSettingsWindow(app, ConfigManager.load(path))
    ui.controls["sample_rate"][1].set_text("invalid")
    assert not ui.save_changes() and ui.page_id == "advanced"
    assert path.read_bytes() == before
    ui.cancel_changes()
    ui.controls["asr_provider"][1].set_active_id("http-cloud")
    assert ui.asr_rows["ws"].get_visible() and ui.asr_rows["http"].get_visible()
    assert not ui.save_changes() and ui.page_id == "asr"
    ui.cancel_changes()
    ui.controls["refine_provider"][1].set_active_id("cloud")
    assert ui.refine_rows["refine_api_model"].get_visible()
    assert not ui.refine_rows["refine_model"].get_visible()
    monkeypatch.setattr(Gtk.MessageDialog, "run", lambda _: Gtk.ResponseType.CANCEL)
    assert ui.close_request() is True
    monkeypatch.setattr(Gtk.MessageDialog, "run", lambda _: Gtk.ResponseType.OK)
    assert ui.close_request() is False
    ui.restore_confirm(None)
    assert ui.draft.dirty and path.read_bytes() == before
    assert "asr_api_key" not in ui.controls
    ui.cancel_changes()
    assert not ui.draft.dirty
    assert path.read_bytes() == before
    ui.window.destroy()


def test_partial_backup_failure_never_publishes_a_backup(tmp_path, monkeypatch):
    import shutil

    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"auto_hard_enter": False})
    before = path.read_bytes()

    def partial(source, output):
        output.write(b'{"truncated":')
        raise OSError("synthetic partial-copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", partial)
    with pytest.raises(ConfigError):
        ConfigManager.save(path, {"auto_hard_enter": True})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup.*"))
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "keyname,expected", [("Print", "<print_screen>"), ("KP_Enter", "<enter>"), ("ISO_Level3_Shift", "<alt_gr>")]
)
def test_native_hotkey_capture_uses_backend_key_names(keyname, expected):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    if not Gtk.init_check()[0]:
        pytest.skip("GTK display required")
    from types import SimpleNamespace

    from recordian.native_settings import NativeSettingsWindow

    captured = []
    entry = SimpleNamespace(set_text=captured.append)
    event = SimpleNamespace(keyval=Gdk.keyval_from_name(keyname), state=0)
    assert NativeSettingsWindow.capture_key(None, entry, event)
    assert captured == [expected]


def test_restart_dispatch_failure_still_reports_saved_config(tmp_path):
    from recordian.settings_draft import SettingsDraft

    path = tmp_path / "hotkey.json"
    ConfigManager.save(path, {"sample_rate": 16000})
    draft = SettingsDraft({"sample_rate": "16000"}, {"sample_rate": "16000"})
    draft.set("sample_rate", "24000")

    def failed_callback():
        raise RuntimeError("synthetic GUI-dispatch failure")

    effect, restarted, keys = draft.persist(path, apply_now=True, restart_callback=failed_callback)
    assert ConfigManager.load(path)["sample_rate"] == 24000
    assert not draft.dirty and not restarted and keys == ["sample_rate"]
