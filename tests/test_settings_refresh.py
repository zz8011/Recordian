"""Offline, synthetic tests. Never construct TrayApp or a real backend."""
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from recordian import settings_catalog as catalog
from recordian.config import ConfigManager


def test_agent_discovery_never_executes_programs_or_reads_personal_config(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    marker = tmp_path / 'executed'
    for name in ('codex', 'claude', 'hermes'):
        cli = bindir / name
        cli.write_text('#!/bin/sh\ntouch ' + str(marker))
        cli.chmod(0o755)
    def forbidden(*args, **kwargs):
        raise AssertionError('must not execute a CLI')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    candidates = catalog.discover_agents(path=str(bindir), home=tmp_path)
    found = {c.kind: c for c in candidates}
    assert found['codex'].executable == str(bindir / 'codex')
    assert found['claude'].executable and found['hermes'].supported
    assert not found['codex'].supported and not marker.exists()
    assert not found['gemini'].executable


def test_microphone_inventory_backend_ids_and_saved_disconnected_value(monkeypatch):
    calls = []
    monkeypatch.setattr(catalog.shutil, 'which', lambda name: '/usr/bin/' + name)
    def query(argv):
        calls.append(argv)
        return '[{"name":"usb.input","description":"USB Mic"},{"name":"sink.monitor","description":"Monitor"}]'
    monkeypatch.setattr(catalog, '_query', query)
    choices = dict(catalog.microphone_choices('ffmpeg-pulse', 'disconnected.input'))
    assert choices['default'] and choices['usb.input'] == 'USB Mic'
    assert 'sink.monitor' not in choices and 'disconnected.input' in choices
    assert calls == [['pactl', '--format=json', 'list', 'sources']]
    calls.clear()
    monkeypatch.setattr(catalog, '_query', lambda argv: calls.append(argv) or 'default\n    Default\nhw:CARD=USB,DEV=0\n    USB capture\nnull\n    Discard\n')
    choices = dict(catalog.microphone_choices('arecord'))
    assert choices['hw:CARD=USB,DEV=0'] == 'USB capture' and 'null' not in choices
    assert calls == [['arecord', '-L']]


def test_failed_inventory_keeps_default_and_configured_device(monkeypatch):
    monkeypatch.setattr(catalog.shutil, 'which', lambda _: '/usr/bin/tool')
    def fail(*_):
        raise subprocess.TimeoutExpired('pactl', 3)
    monkeypatch.setattr(catalog, '_query', fail)
    assert set(dict(catalog.microphone_choices('auto', 'saved-id'))) == {'default', 'saved-id'}


def test_languages_follow_provider_and_preserve_unknown_saved_value():
    assert set(dict(catalog.language_choices('confucius-asr'))) == {'auto', 'Chinese', 'English'}
    assert dict(catalog.language_choices('http-cloud', 'custom')) == {'auto': '自动识别', 'custom': '已配置 · custom'}


def test_autostart_query_is_read_only(monkeypatch):
    calls = []
    monkeypatch.setattr(catalog, '_query', lambda argv: calls.append(argv) or 'enabled\n')
    assert catalog.read_autostart() is True
    assert calls == [['systemctl', '--user', 'show', 'recordian-desktop.service', '--property=UnitFileState', '--value']]


class Store:
    def __init__(self):
        self.values = {'start_on_login': False, 'agent_client': 'hermes', 'agent_executable': '', 'agent_workspace': '/tmp'}
        self.writes = []
        self.fail = False
    def load(self):
        return deepcopy(self.values)
    def save(self, values):
        if self.fail:
            raise OSError('fixture store failure')
        self.values = deepcopy(values)
        self.writes.append(deepcopy(values))


@pytest.fixture
def ui(tmp_path, monkeypatch):
    gi = pytest.importorskip('gi')
    gi.require_version('Gtk', '3.0')
    from gi.repository import GLib, Gtk
    if not Gtk.init_check()[0]:
        pytest.skip('virtual GTK display required')
    from recordian import native_settings as native
    monkeypatch.setattr(native, 'discover_agents', lambda: [catalog.AgentCandidate(k, n, '/synthetic/' + k if k in {'hermes', 'codex', 'claude'} else '', k in {'hermes', 'claude'}) for k, n in catalog.AGENTS])
    current = {
        'asr_provider': 'confucius-asr', 'asr_realtime_endpoint': 'ws://127.0.0.1:8321/asr_stream_api_v1',
        'input_device': 'saved-missing-device', 'qwen_language': 'Chinese',
        'asr_context': 'One\nTwo', 'refine_provider': 'cloud',
        'refine_api_base': 'https://example.invalid/v1', 'refine_api_model': 'fixture-model',
        'refine_api_key': 'fixture-secret', 'refine_model': '/old/model.gguf',
        'enable_remote_paste': True, 'remote_paste_key': 'fixture-pair', 'remote_paste_host': '192.0.2.9',
        'custom': {'retain': True},
    }
    path = tmp_path / 'hotkey.json'
    ConfigManager.save(path, current)
    store = Store()
    restarts = []
    app = SimpleNamespace(config_path=path, settings_preview=True, settings_extension_store=store,
        state=SimpleNamespace(status='idle'), _glib=GLib, _gtk=Gtk, _gtk_settings_window=None,
        root=SimpleNamespace(after=lambda _, fn: fn()), backend=SimpleNamespace(restart=lambda: restarts.append(True)),
        _invalidate_config_cache=lambda: None, _update_tray_menu=lambda: None)
    window = native.NativeSettingsWindow(app, current)
    window.test_restarts = restarts
    yield window
    if not window.closed:
        window.window.destroy()


def pump():
    from gi.repository import GLib
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)


def test_navigation_text_editor_and_removed_entries(ui):
    from gi.repository import Gtk
    assert set(ui.nav) == {'preferences', 'daily', 'input', 'asr', 'refine', 'hotkeys', 'wake', 'agent', 'recording', 'diagnostics'}
    assert isinstance(ui.controls['asr_context'][1], Gtk.TextView)
    assert isinstance(ui.controls['input_device'][1], Gtk.ComboBoxText)
    assert ui.controls['input_device'][1].get_active_id() == 'saved-missing-device'
    assert ui.controls['qwen_language'][1].get_active_id() == 'Chinese'
    assert not any('remote_paste' in key for key in ui.draft.values)
    assert 'refine_provider' not in ui.controls
    for ident in ui.nav:
        ui.navigate(ident)
        assert ui.stack.get_visible_child_name() == ident
    assert ui.close_button.get_accessible().get_name() == '关闭设置'


def test_save_cancel_defaults_preserve_secrets_hidden_remote_and_unknown_values(ui, monkeypatch):
    from gi.repository import Gtk
    path = ui.app.config_path
    original = ConfigManager.load(path)
    before = path.read_bytes()
    ui.controls['asr_context'][1].get_buffer().set_text('Three\nFour → Five')
    ui.extension_controls['start_on_login'][1].set_active(True)
    assert path.read_bytes() == before and not ui.app.settings_extension_store.writes
    ui.cancel_changes()
    assert not ui.draft.dirty and not ui.extension.dirty and path.read_bytes() == before
    monkeypatch.setattr(Gtk.MessageDialog, 'run', lambda _: Gtk.ResponseType.OK)
    ui.restore_confirm(None)
    assert ui.draft.dirty and path.read_bytes() == before
    ui.cancel_changes()
    ui.controls['asr_context'][1].get_buffer().set_text('Three\nFour → Five')
    ui.extension_controls['start_on_login'][1].set_active(True)
    assert ui.save_changes()
    saved = ConfigManager.load(path)
    assert saved['asr_context'] == 'Three\nFour → Five'
    for key in ('refine_api_key', 'refine_model', 'enable_remote_paste', 'remote_paste_key', 'remote_paste_host', 'custom'):
        assert saved[key] == original[key]
    assert ui.app.settings_extension_store.values['start_on_login'] is True
    assert not ui.draft.dirty and not ui.extension.dirty
    assert ui.test_restarts == [True]
    ui.controls['sample_rate'][1].set_text('invalid')
    assert not ui.save_changes() and ui.page_id == 'recording'
    ui.cancel_changes()


def test_busy_save_and_primary_failure_do_not_change_extra_preferences(ui, monkeypatch):
    ui.controls['sample_rate'][1].set_text('24000')
    ui.extension_controls['start_on_login'][1].set_active(True)
    before = ui.app.config_path.read_bytes()
    ui.app.state.status = 'recording'
    assert not ui.save_changes()
    assert ui.app.config_path.read_bytes() == before and not ui.app.settings_extension_store.writes
    ui.app.state.status = 'idle'
    def fail(*args, **kwargs):
        raise OSError('fixture primary config failure')
    monkeypatch.setattr(ui.draft, 'persist', fail)
    assert not ui.save_changes() and ui.draft.dirty and ui.extension.dirty
    assert ui.app.config_path.read_bytes() == before
    assert ui.app.settings_extension_store.values['start_on_login'] is False
    assert ui.test_restarts == []


def test_extension_failure_does_not_write_main_config(ui):
    ui.controls['auto_hard_enter'][1].set_active(True)
    ui.extension_controls['start_on_login'][1].set_active(True)
    before = ui.app.config_path.read_bytes()
    ui.app.settings_extension_store.fail = True
    assert not ui.save_changes() and ui.draft.dirty and ui.extension.dirty
    assert ui.app.config_path.read_bytes() == before and not ui.test_restarts


def test_agent_selection_paths_manual_override_cancel_and_reopen(ui):
    from recordian.native_settings import NativeSettingsWindow
    combo = ui.extension_controls['agent_client'][1]
    rows = {row[1]: row for row in combo.get_model()}
    assert rows['hermes'][2] and not rows['codex'][2]
    combo.set_active_id('codex')
    assert ui.extension.values['agent_client'] == 'hermes'
    assert '尚未支持' in ui.status.get_text()
    ui.extension_controls['agent_executable'][1].set_text('/manual/hermes')
    assert ui.save_changes()
    ui.extension_controls['agent_executable'][1].set_text('/different/hermes')
    ui.cancel_changes()
    assert ui.extension.values['agent_executable'] == '/manual/hermes'
    another = NativeSettingsWindow(ui.app, ConfigManager.load(ui.app.config_path))
    try:
        assert another.extension.values['agent_executable'] == '/manual/hermes'
        assert another.controls['refine_api_key'][1].get_text() == ''
    finally:
        another.window.destroy()


def test_api_migration_is_explicit_and_cancel_keeps_old_model(ui):
    ui.draft.saved['refine_provider'] = ui.draft.values['refine_provider'] = 'llamacpp'
    ui.sync()
    before = ui.app.config_path.read_bytes()
    assert ui.legacy_refine.get_visible()
    assert not ui.refine_rows['refine_api_base'].get_visible()
    ui.changed('refine_provider', 'cloud')
    assert not ui.legacy_refine.get_visible() and ui.draft.dirty
    ui.cancel_changes()
    assert ui.draft.values['refine_provider'] == 'llamacpp'
    assert ui.draft.values['refine_model'] == '/old/model.gguf'
    assert ui.app.config_path.read_bytes() == before


def test_close_decline_preserves_draft_and_explicit_close_discards(ui, monkeypatch):
    from gi.repository import Gtk
    ui.controls['auto_hard_enter'][1].set_active(True)
    before = ui.app.config_path.read_bytes()
    monkeypatch.setattr(Gtk.MessageDialog, 'run', lambda _: Gtk.ResponseType.CANCEL)
    ui.close_button.emit('clicked')
    assert not ui.closed and ui.draft.dirty
    monkeypatch.setattr(Gtk.MessageDialog, 'run', lambda _: Gtk.ResponseType.OK)
    ui.close_button.emit('clicked')
    assert ui.closed and ui.app.config_path.read_bytes() == before


def test_preview_discovery_does_not_send_request(ui, monkeypatch):
    from recordian import native_settings
    def forbidden(*args, **kwargs):
        raise AssertionError('preview must never request a model service')
    monkeypatch.setattr(native_settings, 'fetch_model_list', forbidden)
    ui.discover_button.emit('clicked')
    assert '合成模型' in ui.discovery_status.get_text()
    assert not ui.draft.dirty


def test_speaker_intro_reuses_window_and_cancel_has_no_recording_or_write(ui, monkeypatch):
    from recordian.tray_speaker_wizard import open_speaker_enrollment_wizard
    before = ui.app.config_path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('intro must not start a thread or audio')
    import recordian.tray_speaker_wizard as wizard
    monkeypatch.setattr(wizard.threading, 'Thread', forbidden)
    open_speaker_enrollment_wizard(ui.app)
    pump()
    first = ui.app._gtk_speaker_window
    assert first.get_style_context().has_class('recordian-settings')
    open_speaker_enrollment_wizard(ui.app)
    pump()
    assert first is ui.app._gtk_speaker_window
    assert ui.app.config_path.read_bytes() == before
    first.destroy()
    assert ui.app._gtk_speaker_window is None


def test_initial_page_and_defaults_preserve_hidden_legacy_model(ui, monkeypatch):
    from gi.repository import Gtk

    from recordian.native_settings import NativeSettingsWindow
    another = NativeSettingsWindow(ui.app, ConfigManager.load(ui.app.config_path), page='recording')
    try:
        assert another.stack.get_visible_child_name() == 'recording'
        monkeypatch.setattr(Gtk.MessageDialog, 'run', lambda _: Gtk.ResponseType.OK)
        another.restore_confirm(None)
        assert another.draft.values['refine_model'] == '/old/model.gguf'
    finally:
        another.window.destroy()



def test_absent_agent_preferences_match_existing_runtime_defaults(ui):
    assert ui.draft.values["enable_agent"] is True
    assert ui.draft.values["wake_to_agent"] is True
    assert ui.controls["enable_agent"][1].get_active()
    assert ui.controls["wake_to_agent"][1].get_active()
