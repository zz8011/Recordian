"""Synthetic executables only: never invoke installed Claude/Codex or a model."""
import json
import sys
import threading
import time
from dataclasses import replace

import pytest

from recordian.agent_entry import ADAPTERS, AgentHub, AgentInstance
from recordian.claude_code_adapter import ClaudeCodeAdapter

SID = '550e8400-e29b-41d4-a716-446655440000'


@pytest.fixture
def instance(tmp_path):
    exe = tmp_path / 'synthetic cli with spaces'
    exe.write_text('#!' + sys.executable + '\n')
    exe.chmod(0o700)
    return AgentInstance.parse({'id': 'claude-fixture', 'kind': 'claude', 'executable': str(exe), 'workspace': str(tmp_path)})


def script(instance, code):
    from pathlib import Path
    Path(instance.executable).write_text('#!' + sys.executable + '\n' + code)


def success_code():
    return ('import sys,json\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\n'
            'Path("received.json").write_text(json.dumps({"argv":sys.argv,"text":prompt}))\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":prompt,"session_id":' + repr(SID) + '}))\n')


def test_claude_protocol_and_injection_safe_stdin_with_exact_resume(instance):
    from pathlib import Path
    script(instance, success_code())
    prompt = '中文\n"; $(touch injected) --continue --resume unrelated'
    events = []
    result = ClaudeCodeAdapter().run(instance, prompt, SID, threading.Event(), events.append)
    received = json.loads((Path(instance.workspace) / 'received.json').read_text())
    assert received['argv'] == [instance.executable, '--print', '--output-format', 'json', '--resume', SID]
    assert received['text'] == prompt and result['text'] == prompt
    assert result['session_id'] == SID and events == [result]
    assert not (Path(instance.workspace) / 'injected').exists()
    assert '--dangerously-skip-permissions' not in received['argv']
    assert ADAPTERS['claude'] is ClaudeCodeAdapter


@pytest.mark.parametrize('sid', ['--continue', '../../personal.jsonl', 'partial-name', SID.replace('-', '')])
def test_invalid_resume_never_launches(instance, sid, monkeypatch):
    import recordian.claude_code_adapter as module
    def forbidden(*_args, **_kwargs):
        raise AssertionError('must not launch a CLI for invalid session')
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)
    with pytest.raises(ValueError, match='会话编号'):
        ClaudeCodeAdapter().run(instance, 'synthetic', sid, threading.Event(), lambda _: None)


@pytest.mark.parametrize('value,code', [
    ({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'errors': ['fixture authentication failure']}, 1),
    ({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'partial', 'session_id': SID, 'permission_denials': [{'tool_name': 'Edit'}]}, 0),
    ({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'not success', 'session_id': SID}, 0),
    ({'type': 'result', 'subtype': 'success', 'result': ['invalid text'], 'session_id': SID}, 0),
    ({'type': 'result', 'subtype': 'success', 'result': 'wrong receipt', 'session_id': '../session.jsonl'}, 0),
    ({'type': 'system', 'session_id': SID}, 0),
    ([], 0),
])
def test_errors_denials_malformed_results_are_never_completed(instance, value, code):
    script(instance, 'import sys\nsys.stdin.read()\nprint(' + repr(json.dumps(value)) + ')\nsys.exit(' + repr(code) + ')\n')
    events = []
    with pytest.raises((ValueError, RuntimeError)):
        ClaudeCodeAdapter().run(instance, 'synthetic', '', threading.Event(), events.append)
    assert not events


def test_invalid_json_and_output_limit_fail_closed(instance):
    script(instance, 'import sys\nsys.stdin.read()\nprint("fixture diagnostics only")\n')
    with pytest.raises(RuntimeError, match='有效 JSON'):
        ClaudeCodeAdapter().run(instance, 'synthetic', '', threading.Event(), lambda _: None)
    script(instance, 'import sys,time\nsys.stdin.read()\nsys.stdout.write("x"*(1024*1024+4096));sys.stdout.flush()\ntime.sleep(30)\n')
    before = time.monotonic()
    with pytest.raises(RuntimeError, match='1 MiB'):
        ClaudeCodeAdapter().run(instance, 'synthetic', '', threading.Event(), lambda _: None)
    assert time.monotonic() - before < 4


@pytest.mark.parametrize('action', ['cancel', 'timeout'])
def test_blocked_stdin_and_inherited_child_pipes_terminate_without_hang(instance, action):
    script(instance, 'import os,time\nif os.fork()==0:\n time.sleep(30)\nelse:\n time.sleep(30)\n')
    cancel = threading.Event()
    timer = threading.Timer(.15, cancel.set)
    if action == 'cancel':
        timer.start()
    before = time.monotonic()
    try:
        with pytest.raises(InterruptedError if action == 'cancel' else TimeoutError):
            ClaudeCodeAdapter().run(replace(instance, timeout_s=.4), '字' * 60000, '', cancel, lambda _: None)
    finally:
        timer.cancel()
    assert time.monotonic() - before < 4


def test_claude_gateway_or_hermes_home_rejected(instance):
    data = {'id': 'fixture', 'kind': 'claude', 'executable': instance.executable, 'workspace': instance.workspace}
    with pytest.raises(ValueError, match='Gateway'):
        AgentInstance.parse({**data, 'transport': 'gateway'})
    with pytest.raises(ValueError, match='自身 CLI'):
        AgentInstance.parse({**data, 'home': instance.workspace})


def test_hub_routes_claude_and_keeps_hermes_sessions_separate(instance, tmp_path):
    script(instance, success_code())
    config = tmp_path / 'agents.json'
    config.write_text(json.dumps({'default_agent': instance.id, 'instances': [
        {'id': instance.id, 'kind': 'claude', 'executable': instance.executable, 'workspace': instance.workspace},
        {'id': 'hermes-fixture', 'kind': 'hermes', 'executable': sys.executable, 'workspace': str(tmp_path)},
    ]}))
    events = []
    hub = AgentHub(config, tmp_path / 'synthetic-tasks.json', on_task_event=lambda task, _name: events.append(task))
    try:
        hub.sessions['hermes-fixture'] = 'fixture-hermes-session'
        task = {'id': 'fixture', 'agent_id': instance.id, 'prompt': 'synthetic task', 'session_id': '', 'reply': ''}
        hub._run(task, threading.Event())
        assert task['status'] == 'completed' and task['reply'] == 'synthetic task'
        assert hub.sessions[instance.id] == SID and hub.sessions['hermes-fixture'] == 'fixture-hermes-session'
        assert events[-1]['event_type'] == 'finished' and events[-1]['status'] == 'completed'
        # A failure produces a failed receipt rather than replaying the prompt.
        script(instance, 'import sys\nsys.stdin.read()\nprint("fixture invalid result")\n')
        second = {'id': 'failure', 'agent_id': instance.id, 'prompt': 'synthetic', 'session_id': '', 'reply': ''}
        hub._run(second, threading.Event())
        assert second['status'] == 'failed' and events[-1]['status'] == 'failed'
        assert '有效 JSON' in second['error']
    finally:
        hub.close()



def test_task_panel_preserves_claude_kind_when_editing_and_saving(tmp_path):
    import re
    import shutil
    import subprocess
    from pathlib import Path

    import recordian.agent_entry as module
    node = shutil.which("node")
    if not node:
        pytest.skip("optional JS runtime unavailable")
    html = Path(module.__file__).with_name("agent_panel.html")
    source = re.search(r"<script>(.*?)</script>", html.read_text(), re.S).group(1)
    panel = tmp_path / "panel-script.js"
    panel.write_text(source)
    runner = tmp_path / "mock-dom.cjs"
    runner.write_text(r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),posts=[];
function element(){return {value:'',options:[],disabled:false,replaceChildren(...children){this.options=children},append(){}}}
function get(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)}
const instance={id:'fixture',name:'Claude fixture',kind:'claude',workspace:'/tmp',executable:'/synthetic/claude',home:'',transport:'cli',api_url:''};
const fixture={instances:[instance],selected:'fixture',tasks:[],sessions:{},phase:'idle',enable_agent:true,wake_to_agent:true,preview:'',notice:''};
const context={document:{getElementById:get,createElement:element},location:{hash:'#synthetic-token',pathname:'/'},sessionStorage:{getItem(){return null},setItem(){}},history:{replaceState(){}},setInterval(){},Option:class{constructor(text,value){this.text=text;this.value=value}},fetch:async(url,opts)=>{if(opts.method==='POST')posts.push([url,JSON.parse(opts.body)]);return {ok:true,json:async()=>fixture}}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
setImmediate(async()=>{try{
get('editInstance').onclick();
assert.equal(get('instanceKind').value,'claude');
assert.equal(get('executable').value,'/synthetic/claude');
assert.equal(get('transport').disabled,true);
assert.ok(get('transportHint').textContent.includes('Claude Code'));
get('home').value='/synthetic/hermes';get('apiUrl').value='http://127.0.0.1:8652';get('transport').value='gateway';get('instanceKind').onchange();
await get('saveInstance').onclick();
assert.equal(posts.length,1);assert.equal(posts[0][0],'/api/instance');
assert.equal(posts[0][1].kind,'claude');assert.equal(posts[0][1].transport,'cli');assert.equal(posts[0][1].home,'');assert.equal(posts[0][1].api_url,'');
}catch(error){process.stderr.write(String(error));process.exitCode=1}});
""")
    subprocess.run([node, str(runner), str(panel)], check=True, capture_output=True, timeout=5)
