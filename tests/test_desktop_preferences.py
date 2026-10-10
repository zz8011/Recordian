"""Temporary profiles, synthetic unit runner, and mock runtime adapters only."""
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from recordian.agent_entry import AgentHub
from recordian.config import ConfigManager
from recordian.desktop_preferences import Autostart, DesktopPreferencesStore


class UnitRunner:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []
        self.fail = False
    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if 'show' in args:
            return SimpleNamespace(stdout='LoadState=loaded\nUnitFileState=' + ('enabled' if self.enabled else 'disabled'))
        assert '--now' not in args and not {'start', 'stop', 'restart'}.intersection(args)
        if self.fail:
            raise OSError('fixture enablement failure')
        self.enabled = 'enable' in args
        return SimpleNamespace(stdout='')


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'agents.json'
    doc = {'default_agent': 'desktop', 'custom': {'retain': True}, 'instances': [
        {'id': 'desktop', 'kind': 'hermes', 'executable': sys.executable, 'workspace': str(tmp_path), 'name': '桌面', 'unknown': 'keep'},
        {'id': 'other', 'kind': 'hermes', 'executable': sys.executable, 'workspace': str(tmp_path), 'name': '另一个'},
    ]}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=3) + '\n')
    unit = UnitRunner()
    obj = DesktopPreferencesStore(path, Autostart(unit))
    assert obj.refresh_autostart() is True
    return obj, unit


def test_read_cancel_and_validate_do_not_mutate_profiles_or_startup(store):
    obj, unit = store
    before = obj.path.read_bytes()
    values = obj.load()
    values['agent_workspace'] = '/not-existing-fixture-path'
    with pytest.raises(ValueError):
        obj.save(values)
    assert obj.path.read_bytes() == before and unit.enabled
    assert all('show' in call for call in unit.calls)


def test_select_supported_instance_and_gateway_preserves_other_profiles(store, tmp_path):
    obj, unit = store
    values = obj.load()
    values.update(obj.select_profile('other'))
    home = tmp_path / 'profile'
    home.mkdir()
    (home / '.env').write_text('fixture file existence only; never read')
    values.update(agent_transport='gateway', agent_home=str(home), agent_api_url='http://127.0.0.1:8811')
    receipt = obj.save(values)
    doc = ConfigManager.load(obj.path)
    assert doc['default_agent'] == 'other'
    assert doc['custom'] == {'retain': True}
    assert doc['instances'][0]['unknown'] == 'keep'
    assert doc['instances'][1]['transport'] == 'gateway'
    assert receipt.agent_changed and not receipt.startup_changed
    assert all('show' in call for call in unit.calls)
    reopened = DesktopPreferencesStore(obj.path, obj.autostart)
    assert reopened.load()['agent_instance'] == 'other'
    assert reopened.load()['agent_api_url'] == 'http://127.0.0.1:8811'


def test_unsupported_kind_and_remote_gateway_rejected_before_write(store):
    obj, unit = store
    before = obj.path.read_bytes()
    values = obj.load()
    values['agent_client'] = 'codex'
    with pytest.raises(ValueError, match='适配器'):
        obj.save(values)
    values = obj.load()
    values.update(agent_transport='gateway', agent_api_url='http://192.0.2.9:8811')
    with pytest.raises(ValueError, match='本机'):
        obj.save(values)
    assert obj.path.read_bytes() == before and unit.enabled


def test_startup_only_changes_existing_enablement_and_rollback_restores_exact_bytes(store):
    obj, unit = store
    before = obj.path.read_bytes()
    values = obj.load()
    values.update(start_on_login=False, agent_name='更新显示名')
    receipt = obj.save(values)
    assert not unit.enabled and obj.path.read_bytes() != before
    obj.restore(receipt)
    assert unit.enabled and obj.path.read_bytes() == before
    mutations = [c for c in unit.calls if 'show' not in c]
    assert [c[2] for c in mutations] == ['disable', 'enable']
    assert obj.path.stat().st_mode & 0o777 == 0o600


def test_startup_failure_and_concurrent_profile_edit_preserve_original(store):
    obj, unit = store
    before = obj.path.read_bytes()
    values = obj.load()
    values['start_on_login'] = False
    unit.fail = True
    with pytest.raises(ValueError):
        obj.save(values)
    assert obj.path.read_bytes() == before and unit.enabled
    unit.fail = False
    obj.path.write_bytes(before + b' ')
    values = obj.load()
    values['agent_name'] = 'new name'
    with pytest.raises(ValueError, match='其他窗口'):
        obj.save(values)
    assert obj.path.read_bytes() == before + b' '


def test_agent_file_failure_compensates_startup_change(store, monkeypatch):
    obj, unit = store
    before = obj.path.read_bytes()
    values = obj.load()
    values.update(start_on_login=False, agent_name='change')
    def fail(*args):
        raise OSError('fixture profile write failure')
    monkeypatch.setattr(ConfigManager, 'save', fail)
    with pytest.raises(OSError):
        obj.save(values)
    assert obj.path.read_bytes() == before and unit.enabled


def test_new_profile_has_unique_identity_and_receipt_removes_only_created_file(store, tmp_path):
    obj, _ = store
    assert obj.select_profile('new')['agent_id'] == 'desktop-2'
    new = DesktopPreferencesStore(tmp_path / 'new-agents.json', Autostart(UnitRunner()))
    values = new.load()
    values.update(agent_executable=sys.executable, agent_workspace=str(tmp_path))
    receipt = new.save(values)
    assert new.path.exists()
    new.restore(receipt)
    assert not new.path.exists()


def test_malformed_profile_is_never_overwritten(tmp_path):
    path = tmp_path / 'agents.json'
    path.write_bytes(b'{fixture invalid JSON')
    with pytest.raises(ValueError, match='损坏'):
        DesktopPreferencesStore(path, Autostart(UnitRunner()))
    assert path.read_bytes() == b'{fixture invalid JSON'


class CLIAdapter:
    calls = []
    def run(self, instance, text, session_id, cancel, on_event):
        self.calls.append(instance.transport)
        return {'text': 'fixture result', 'exit_code': 0}


class GatewayAdapter(CLIAdapter):
    def run(self, instance, text, session_id, cancel, on_event, request_id):
        return super().run(instance, text, session_id, cancel, on_event)


def test_runtime_reloads_profiles_only_when_idle_without_cancelling_or_replaying(store, tmp_path):
    obj, _ = store
    hub = AgentHub(obj.path, tmp_path/'fixture-tasks.json', adapters={'hermes': CLIAdapter, 'hermes-gateway': GatewayAdapter})
    try:
        hub.sessions = {'desktop': 'fixture-session', 'other': 'other-fixture-session'}
        capture = hub.begin_capture()
        values = obj.load()
        values.update(obj.select_profile('other'))
        obj.save(values)
        hub._refresh_preferences()
        assert capture.agent_id == 'desktop' and hub.selected == 'desktop'
        hub.end_capture(capture)
        hub._refresh_preferences()
        assert hub.selected == 'other' and hub.sessions['desktop'] == 'fixture-session'
        hub.cancels['other'] = threading.Event()
        values = obj.load()
        values.update(obj.select_profile('desktop'))
        home = tmp_path/'gateway-profile'
        home.mkdir()
        (home/'.env').write_text('fixture existence only')
        values.update(agent_transport='gateway', agent_home=str(home), agent_api_url='http://127.0.0.1:8811')
        obj.save(values)
        hub._refresh_preferences()
        assert hub.selected == 'other' and hub.instances['desktop'].transport == 'cli'
        hub.cancels.clear()
        hub._refresh_preferences()
        assert hub.selected == 'desktop' and hub.instances['desktop'].transport == 'gateway'
        assert 'desktop' not in hub.sessions and hub.sessions['other'] == 'other-fixture-session'
        assert not hub.tasks
        CLIAdapter.calls = []
        task = {'id': 'fixture', 'agent_id': 'desktop', 'prompt': 'synthetic test', 'session_id': '', 'reply': ''}
        hub._run(task, threading.Event())
        assert task['status'] == 'completed' and CLIAdapter.calls == ['gateway']
    finally:
        hub.cancels.clear()
        hub.close()


def test_runtime_invalid_profiles_block_new_agent_tasks_and_recover(store, tmp_path):
    obj, _ = store
    hub = AgentHub(obj.path, tmp_path/'fixture-tasks.json', adapters={'hermes': CLIAdapter})
    try:
        before = obj.path.read_bytes()
        obj.path.write_bytes(b'{fixture invalid JSON')
        hub._refresh_preferences()
        assert not hub.enabled and hub._profile_error
        with pytest.raises(ValueError):
            hub.submit('synthetic', 'desktop')
        obj.path.write_bytes(before)
        hub._refresh_preferences()
        assert hub.enabled and not hub._profile_error
        assert not hub.tasks
    finally:
        hub.close()


@pytest.fixture
def native_store_ui(store, tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk
    if not Gtk.init_check()[0]:
        pytest.skip("virtual GTK display required")
    from recordian.native_settings import NativeSettingsWindow
    obj, unit = store
    config = tmp_path / "hotkey.json"
    current = {"refine_provider": "cloud", "refine_api_base": "https://example.invalid/v1", "refine_api_model": "fixture-model", "custom": {"retain": True}}
    ConfigManager.save(config, current)
    app = SimpleNamespace(config_path=config, settings_extension_store=obj, settings_preview=False,
        state=SimpleNamespace(status="idle"), _gtk_settings_window=None,
        root=SimpleNamespace(after=lambda *_: None), backend=SimpleNamespace(restart=lambda: None),
        _invalidate_config_cache=lambda: None, _update_tray_menu=lambda: None)
    window = NativeSettingsWindow(app, current, page="agent")
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)
    yield window, obj, unit
    window.window.destroy()


def test_native_save_uses_real_store_and_reopen_keeps_saved_profile(native_store_ui):
    ui, obj, unit = native_store_ui
    before = obj.path.read_bytes()
    ui.extension_controls["agent_name"][1].set_text("设置面板保存的实例")
    ui.extension_controls["start_on_login"][1].set_active(False)
    ui.controls["auto_hard_enter"][1].set_active(True)
    assert obj.path.read_bytes() == before and unit.enabled
    assert ui.save_changes()
    assert not unit.enabled and obj.load()["agent_name"] == "设置面板保存的实例"
    assert ConfigManager.load(ui.app.config_path)["custom"] == {"retain": True}
    reopened = DesktopPreferencesStore(obj.path, Autostart(unit))
    assert reopened.load()["agent_name"] == "设置面板保存的实例"
    assert reopened.refresh_autostart() is False
    assert not ui.extension.dirty and not ui.draft.dirty


def test_native_primary_failure_rolls_back_real_profile_and_enablement(native_store_ui, monkeypatch):
    ui, obj, unit = native_store_ui
    main_before, agent_before = ui.app.config_path.read_bytes(), obj.path.read_bytes()
    ui.extension_controls["agent_name"][1].set_text("取消的实例名")
    ui.extension_controls["start_on_login"][1].set_active(False)
    ui.controls["auto_hard_enter"][1].set_active(True)
    def fail(*_args, **_kwargs):
        raise OSError("fixture primary failure")
    monkeypatch.setattr(ui.draft, "persist", fail)
    assert not ui.save_changes()
    assert ui.app.config_path.read_bytes() == main_before and obj.path.read_bytes() == agent_before and unit.enabled
    assert ui.extension.dirty and ui.draft.dirty
    ui.cancel_changes()
    assert not ui.extension.dirty and not ui.draft.dirty


def test_unit_timeout_after_change_compensates_once_and_reports_failure():
    class PartialRunner(UnitRunner):
        def __call__(self, args, **kwargs):
            result = super().__call__(args, **kwargs)
            if "disable" in args:
                raise TimeoutError("fixture timeout after disable")
            return result
    runner = PartialRunner()
    unit = Autostart(runner)
    with pytest.raises(ValueError, match="未保存"):
        unit.set(False, True)
    assert runner.enabled
    assert [c[2] for c in runner.calls if "show" not in c] == ["disable", "enable"]


def test_failed_startup_compensation_is_explicit_and_bounded():
    class FailureRunner(UnitRunner):
        def __call__(self, args, **kwargs):
            if "enable" in args:
                self.calls.append(args)
                raise OSError("fixture compensation denied")
            result = super().__call__(args, **kwargs)
            if "disable" in args:
                raise TimeoutError("fixture timeout after disable")
            return result
    runner = FailureRunner()
    with pytest.raises(ValueError, match="未能回退"):
        Autostart(runner).set(False, True)
    assert not runner.enabled
    assert [c[2] for c in runner.calls if "show" not in c] == ["disable", "enable"]



def test_native_claude_selection_uses_supported_adapter_and_real_profile_store(native_store_ui):
    ui, obj, _unit = native_store_ui
    from recordian.agent_entry import AgentInstance
    combo = ui.extension_controls["agent_client"][1]
    rows = {row[1]: row for row in combo.get_model()}
    assert rows["claude"][2] and not rows["codex"][2]
    combo.set_active_id("claude")
    assert ui.extension.values["agent_client"] == "claude"
    assert not ui.agent_home_row.get_visible() and not ui.transport_row.get_visible()
    assert ui.extension.values["agent_transport"] == "cli"
    assert ui.extension.values["agent_home"] == "" and ui.extension.values["agent_api_url"] == ""
    ui.extension_controls["agent_executable"][1].set_text(sys.executable)
    assert ui.save_changes()
    saved = ConfigManager.load(obj.path)
    profile = saved["instances"][0]
    assert AgentInstance.parse(profile).kind == "claude"
    assert saved["instances"][1]["kind"] == "hermes" and profile["unknown"] == "keep"
    reopened = DesktopPreferencesStore(obj.path, obj.autostart)
    assert reopened.load()["agent_client"] == "claude"
    assert "desktop" in dict(reopened.profiles())
    assert reopened.select_profile("desktop")["agent_client"] == "claude"
    assert ui.controls["refine_api_base"][1].get_text() == "https://example.invalid/v1"
