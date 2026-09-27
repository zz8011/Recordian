from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import pytest

from recordian.agent_entry import AgentHub, AgentInstance, HermesAdapter
from recordian.agent_panel import AgentPanel
from recordian.postprocess_pipeline import _resolve_auto_hard_enter


class FakeAdapter:
    calls = []
    def run(self, instance, text, session_id, cancel, on_event):
        self.calls.append((instance.id, text, session_id))
        sid = session_id or 'session-' + instance.id
        on_event({'type': 'system', 'session_id': sid})
        on_event({'type': 'text', 'text': '答复'})
        return {'type': 'result', 'exit_code': 0, 'session_id': sid, 'text': '完成：' + text}


@pytest.fixture
def hub(tmp_path):
    FakeAdapter.calls = []
    config = tmp_path / 'agents.json'
    config.write_text(json.dumps({'default_agent': 'hermes', 'instances': [
        {'id': 'hermes', 'kind': 'hermes', 'executable': sys.executable, 'workspace': str(tmp_path)},
        {'id': 'other', 'kind': 'hermes', 'executable': sys.executable, 'workspace': str(tmp_path)},
    ]}))
    obj = AgentHub(config, tmp_path/'tasks.json', adapters={'hermes': FakeAdapter})
    yield obj
    obj.close()


def wait_done(hub):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not hub.cancels:
            return
        time.sleep(.01)
    raise AssertionError('task did not finish')


def test_partial_text_does_not_dispatch_or_touch_desktop(hub):
    hub.select('agent', 'hermes')
    capture = hub.begin_capture()
    session = capture.sink.begin_composition()
    assert session.update_preedit('写错的中间文字').committed
    assert session.commit_segment('修正后的前半句').committed
    assert session.commit('后半句').committed
    assert FakeAdapter.calls == []
    payload = {'result': {'text': '完整修正后的指令', 'commit': {'outcome': 'committed'}}}
    hub.accept_transcript(capture, payload)
    hub.accept_transcript(capture, payload)
    wait_done(hub)
    assert FakeAdapter.calls == [('hermes', '完整修正后的指令', '')]
    hub.end_capture(capture)


def test_dictation_never_submits_and_destination_is_pinned(hub):
    capture = hub.begin_capture()
    with pytest.raises(ValueError):
        hub.select('agent', 'other')
    hub.accept_transcript(capture, {'result': {'text': '普通打字'}})
    assert not hub.tasks
    hub.end_capture(capture)
    hub.select('agent', 'other')
    assert hub.begin_capture().agent_id == 'other'


@pytest.mark.parametrize('outcome,failed', [('uncertain', False), ('cancelled', False), ('stale', False), ('committed', True)])
def test_incomplete_asr_never_submits(hub, outcome, failed):
    hub.select('agent', 'hermes')
    capture = hub.begin_capture()
    hub.accept_transcript(capture, {'result': {'text': '不完整指令', 'commit': {'outcome': outcome}}}, failed=failed)
    assert not hub.tasks


def test_capture_disables_remote_paste_and_even_live_config_enter(hub, tmp_path):
    path = tmp_path/'hotkey.json'
    path.write_text('{"auto_hard_enter":true}')
    args = argparse.Namespace(enable_remote_paste=True, enable_streaming_commit=False, auto_hard_enter=True, config_path=str(path))
    hub.select('agent', 'hermes')
    routed = hub.begin_capture().arguments(args)
    assert not routed.enable_remote_paste
    assert routed.enable_streaming_commit
    assert not _resolve_auto_hard_enter(routed)
    assert args.enable_remote_paste and args.auto_hard_enter


def test_instances_resume_only_their_own_session_and_deduplicate(hub):
    job = hub.submit('第一条', 'hermes', 'request-1')
    wait_done(hub)
    assert hub.submit('第一条', 'hermes', 'request-1') == job
    with pytest.raises(ValueError):
        hub.submit('不同指令', 'hermes', 'request-1')
    hub.submit('另一个任务', 'other')
    wait_done(hub)
    hub.submit('继续', 'hermes')
    wait_done(hub)
    assert FakeAdapter.calls[-1] == ('hermes', '继续', 'session-hermes')
    hub.new_session('hermes')
    assert 'hermes' not in hub.sessions


def test_restart_never_replays_and_does_not_reuse_changed_workspace(hub, tmp_path):
    hub.submit('任务', 'hermes')
    wait_done(hub)
    state = json.loads(hub.state_path.read_text())
    state['tasks'][0]['status'] = 'running'
    hub.state_path.write_text(json.dumps(state))
    config = json.loads(hub.config_path.read_text())
    newdir = tmp_path/'another'
    newdir.mkdir()
    config['instances'][0]['workspace'] = str(newdir)
    hub.config_path.write_text(json.dumps(config))
    reloaded = AgentHub(hub.config_path, hub.state_path, adapters={'hermes': FakeAdapter})
    assert reloaded.mode == 'dictation'
    assert reloaded.tasks[0]['status'] == 'interrupted'
    assert 'hermes' not in reloaded.sessions
    assert len(FakeAdapter.calls) == 1
    assert reloaded.state_path.stat().st_mode & 0o777 == 0o600
    reloaded.close()


def test_busy_and_cancel_are_explicit(hub):
    class Blocking:
        def run(self, instance, text, session_id, cancel, on_event):
            assert cancel.wait(2)
            raise InterruptedError('已停止')
    hub.adapters = {'hermes': Blocking}
    hub.submit('慢任务', 'hermes')
    with pytest.raises(ValueError):
        hub.submit('不要重复执行', 'hermes')
    hub.select('agent', 'hermes')
    with pytest.raises(RuntimeError):
        hub.begin_capture()
    hub.cancel('hermes')
    wait_done(hub)
    assert hub.tasks[-1]['status'] == 'cancelled'


def test_real_subprocess_preserves_literal_prompt_and_requires_terminal(tmp_path):
    executable = tmp_path/'hermes'
    executable.write_text('#!'+sys.executable+'\nimport sys,json\ns=sys.stdin.read()\nprint(json.dumps({"type":"result","exit_code":0,"session_id":"exact-session","text":s}))\n')
    executable.chmod(0o700)
    instance = AgentInstance('test', 'Test', 'hermes', str(executable), str(tmp_path), timeout_s=30)
    prompt = '引号\"、反引号 `touch bad`、$(touch bad)、换行\n完整保留'
    result = HermesAdapter().run(instance, prompt, '', threading.Event(), lambda e: None)
    assert result['text'] == prompt
    assert not (tmp_path/'bad').exists()
    executable.write_text('#!'+sys.executable+'\nprint("no terminal record")\n')
    with pytest.raises(RuntimeError, match='未返回最终结果'):
        HermesAdapter().run(instance, 'test', '', threading.Event(), lambda e: None)


def test_panel_requires_token_and_rejects_cross_origin(hub, tmp_path):
    import urllib.error
    import urllib.request
    panel = AgentPanel(hub, path=tmp_path/'address.json', record=lambda: None)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(panel.url+'/api/state')
        assert err.value.code == 403
        headers = {'X-Recordian-Token': panel.token}
        request = urllib.request.Request(panel.url+'/api/state', headers=headers)
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['mode'] == 'dictation'
        request = urllib.request.Request(panel.url+'/api/select', data=b'{"mode":"agent","agent_id":"hermes"}', headers={**headers, 'Origin': 'https://example.com'})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request)
        assert err.value.code == 403
        assert hub.mode == 'dictation'
    finally:
        panel.close()


def test_cancel_unblocks_full_stdin_and_terminates_group(tmp_path):
    executable = tmp_path/'hermes'
    ready = tmp_path/'ready'
    executable.write_text('#!'+sys.executable+'\nfrom pathlib import Path\nimport time\nPath("ready").touch()\ntime.sleep(30)\n')
    executable.chmod(0o700)
    instance = AgentInstance('test', 'Test', 'hermes', str(executable), str(tmp_path), timeout_s=30)
    cancel = threading.Event()
    failures = []
    def run():
        try:
            HermesAdapter().run(instance, '字'*60000, '', cancel, lambda e: None)
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic()+3
    while not ready.exists() and time.monotonic()<deadline:
        time.sleep(.01)
    assert ready.exists()
    cancel.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(failures)==1 and isinstance(failures[0], InterruptedError)


@pytest.mark.parametrize('panel_mode', ['agent', 'dictation'])
def test_wake_and_keyboard_route_independently_of_panel(hub, panel_mode):
    hub.select(panel_mode, 'hermes')
    for source, mode in [('voice_wake', 'agent'), ('hotkey', 'dictation'), ('agent_panel', 'agent')]:
        capture = hub.begin_capture(source)
        assert capture.mode == mode
        with pytest.raises(ValueError):
            hub.select(panel_mode, 'other')
        hub.accept_transcript(capture, {'result': {'text': source, 'commit': {'outcome': 'committed'}}})
        hub.end_capture(capture)
        wait_done(hub)
    assert [c[1] for c in FakeAdapter.calls] == ['voice_wake', 'agent_panel']


def test_busy_agent_does_not_block_keyboard_dictation(hub):
    hub.select('agent', 'hermes')
    hub.cancels['hermes'] = threading.Event()
    with pytest.raises(RuntimeError, match='正在执行'):
        hub.begin_capture('voice_wake')
    capture = hub.begin_capture('hotkey')
    assert capture.mode == 'dictation'
    hub.end_capture(capture)
    hub.cancels.clear()
