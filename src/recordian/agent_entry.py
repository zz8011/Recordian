"""Voice task routing independent of an agent's model/provider.

Hermes can use its JSONL CLI or a persistent local Gateway. No UI keystrokes or
shell interpolation are used. A recording pins its destination until finalization.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .linux_commit import CommitResult

MAX_TEXT = 60000
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


def private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, filename = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    tmp = Path(filename)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class AgentInstance:
    id: str
    name: str
    kind: str
    executable: str
    workspace: str
    home: str = ''
    timeout_s: int = 1800
    transport: str = 'cli'
    api_url: str = ''

    @classmethod
    def parse(cls, data: dict) -> AgentInstance:
        ident = str(data.get('id', ''))
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', ident):
            raise ValueError('Agent 标识需为小写英文、数字、横线或下划线')
        if data.get('kind', 'hermes') != 'hermes':
            raise ValueError('当前版本已接通 Hermes，其他适配器尚未安装')
        transport = str(data.get('transport', 'cli'))
        if transport not in {'cli', 'gateway'}:
            raise ValueError('Hermes 连接方式无效')
        api_url = str(data.get('api_url', '')).rstrip('/')
        if transport == 'gateway':
            parsed = urllib.parse.urlparse(api_url)
            if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('Gateway 地址必须是本机 HTTP 地址和端口')
        workspace = Path(str(data.get('workspace', ''))).expanduser()
        executable = Path(str(data.get('executable', ''))).expanduser()
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError('请选择存在的绝对工作目录')
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('Hermes 启动程序不存在或不能执行')
        home = str(data.get('home', '')).strip()
        if home:
            hp = Path(home).expanduser()
            if not hp.is_absolute() or not hp.is_dir():
                raise ValueError('Hermes 配置目录不存在')
            home = str(hp.resolve())
        if transport == 'gateway' and (not home or not (Path(home) / '.env').is_file()):
            raise ValueError('Gateway 需要包含 .env 的 Hermes profile 目录')
        return cls(ident, str(data.get('name', ident))[:80], 'hermes', str(executable.resolve()),
                   str(workspace.resolve()), home, max(30, min(7200, int(data.get('timeout_s', 1800)))),
                   transport, api_url)


class HermesAdapter:
    """One turn per process; resume only the exact ID returned by Hermes."""
    def run(self, instance, text, session_id, cancel, on_event):
        argv = [instance.executable, 'chat', '--query-file', '-', '--oneshot', '--format', 'stream-json',
                '--in', instance.workspace, '--source', 'recordian', '--run-budget', str(instance.timeout_s)]
        if session_id:
            argv += ['--resume', session_id, '--no-restore-cwd']
        env = os.environ.copy()
        if instance.home:
            env['HERMES_HOME'] = instance.home
        proc = subprocess.Popen(argv, cwd=instance.workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        events = queue.Queue(maxsize=128)
        errors = bytearray()
        diagnostics = bytearray()
        faults = []
        finished = threading.Event()

        def reader():
            try:
                total = 0
                while True:
                    line = proc.stdout.readline(262145)
                    if not line:
                        break
                    if len(line) > 262144:
                        raise ValueError('Hermes 单条事件过大')
                    total += len(line)
                    if total > 64 * 1024 * 1024:
                        raise ValueError('Hermes 事件总量超过本次任务上限')
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        diagnostics.extend(line)
                        del diagnostics[:-8192]
                        continue  # package diagnostics may precede JSONL
                    if isinstance(value, dict):
                        while not finished.is_set():
                            try:
                                events.put(value, timeout=.1)
                                break
                            except queue.Full:
                                pass
            except Exception as exc:
                faults.append(str(exc))
            finally:
                finished.set()

        def stderr_reader():
            try:
                while chunk := proc.stderr.read(4096):
                    errors.extend(chunk)
                    del errors[:-8192]
            except (OSError, ValueError):
                pass

        def writer():
            try:
                proc.stdin.write(text.encode('utf-8'))
                proc.stdin.close()
            except (OSError, ValueError):
                faults.append('Hermes 未完整接收指令，任务不会自动重试')

        out_thread = threading.Thread(target=reader, daemon=True)
        err_thread = threading.Thread(target=stderr_reader, daemon=True)
        out_thread.start()
        err_thread.start()
        in_thread = threading.Thread(target=writer, daemon=True)
        in_thread.start()
        terminal = None
        deadline = time.monotonic() + instance.timeout_s + 15
        try:
            while not finished.is_set() or not events.empty() or proc.poll() is None:
                if cancel.is_set():
                    raise InterruptedError('任务已停止；已经执行的操作不会自动撤销')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Agent 执行超时，请检查任务记录后决定是否继续')
                if faults:
                    raise RuntimeError(faults[0])
                try:
                    event = events.get(timeout=.1)
                except queue.Empty:
                    continue
                if event.get('type') == 'result':
                    terminal = event
                on_event(event)
            code = proc.wait(timeout=2)
            if faults:
                raise RuntimeError(faults[0])
            if terminal is None:
                err_thread.join(timeout=.5)
                detail = (bytes(errors) + bytes(diagnostics)).decode('utf-8', errors='replace').lower()
                if ('hermes-refusal-reason: session_not_owned' in detail
                        or 'this chat is open in another hermes window/terminal' in detail
                        or 'refused active session' in detail
                        or ('session' in detail and 'already held' in detail)):
                    raise RuntimeError('Hermes 对话正在桌面端使用。请在 Recordian Agent 面板点击“开始新会话”，再决定是否重发；本次不会自动重试')
                raise RuntimeError(f'Hermes 未返回最终结果（退出码 {code}），请检查 Hermes 配置或审批要求')
            if code != 0 or terminal.get('exit_code') != 0:
                raise RuntimeError(str(terminal.get('error') or f'Hermes 执行未完成（退出码 {code}）')[:1500])
            return terminal
        finally:
            finished.set()
            # A launcher can exit while one of its children still owns a pipe.
            # Terminate our whole group before joining/closing buffered IO;
            # closing a pipe while its reader holds the lock can otherwise hang.
            workers = (out_thread, err_thread, in_thread)
            if proc.poll() is None or any(t.is_alive() for t in workers):
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for thread in workers:
                    thread.join(timeout=.5)
                if proc.poll() is None or any(t.is_alive() for t in workers):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            proc.wait(timeout=3)
            for thread in workers:
                thread.join(timeout=.5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed and not any(t.is_alive() for t in workers):
                    stream.close()


class HermesGatewayAdapter:
    """Use a persistent local Gateway; never resubmit an uncertain instruction."""

    @staticmethod
    def _key(home):
        for line in (Path(home) / '.env').read_text().splitlines():
            if line.startswith('API_SERVER_KEY='):
                key = line.partition('=')[2].strip().strip('"\'')
                if key:
                    return key
        raise RuntimeError('Hermes Gateway profile 未设置 API_SERVER_KEY')

    def run(self, instance, text, session_id, cancel, on_event, *, request_id=None):
        base = instance.api_url
        key = self._key(instance.home)
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}

        def request(method, path, data=None, timeout=5):
            payload = None if data is None else json.dumps(data, ensure_ascii=False).encode()
            req = urllib.request.Request(base + path, data=payload, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                detail = exc.read(1024).decode(errors='replace')
                raise RuntimeError(f'Hermes Gateway 返回 {exc.code}: {detail}') from exc

        if cancel.is_set():
            raise InterruptedError('任务已停止，指令尚未发送')
        body = {'input': text}
        if session_id:
            body['session_id'] = session_id
        # A POST may have reached Hermes even if the reply is lost. Never retry it here.
        create_headers = dict(headers)
        create_headers['Idempotency-Key'] = request_id or uuid.uuid4().hex
        create = urllib.request.Request(base + '/v1/runs', data=json.dumps(body, ensure_ascii=False).encode(),
                                        method='POST', headers=create_headers)
        try:
            with urllib.request.urlopen(create, timeout=10) as response:
                started = json.load(response)
        except Exception as exc:
            raise RuntimeError('Hermes Gateway 提交状态不明，请检查任务记录；不会自动重发') from exc
        run_id = started.get('run_id')
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', run_id):
            raise RuntimeError('Hermes Gateway 未返回有效任务编号；不会自动重发')
        path = '/v1/runs/' + run_id
        stream_events = queue.Queue(maxsize=256)
        stop_stream = threading.Event()

        def stream():
            req = urllib.request.Request(base + path + '/events', headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    for line in response:
                        if stop_stream.is_set():
                            return
                        if not line.startswith(b'data: '):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(event, dict):
                            try:
                                stream_events.put_nowait(event)
                            except queue.Full:
                                pass  # Polling supplies the authoritative final state.
            except (OSError, urllib.error.URLError):
                pass  # Polling continues even when the optional stream disconnects.

        thread = threading.Thread(target=stream, daemon=True)
        thread.start()
        deadline = time.monotonic() + instance.timeout_s + 15
        next_poll = 0.0
        status = None
        seen_session = ''
        try:
            while True:
                if cancel.is_set():
                    try:
                        request('POST', path + '/stop', {}, timeout=3)
                    except (OSError, RuntimeError) as exc:
                        raise RuntimeError('Hermes 停止请求未确认，请检查任务状态；不会自动重发') from exc
                    stop_deadline = time.monotonic() + 15
                    while time.monotonic() < stop_deadline:
                        stopped = request('GET', path, timeout=3)
                        if stopped.get('status') in {'cancelled', 'interrupted'}:
                            raise InterruptedError('Hermes 任务已停止；已经执行的操作不会自动撤销')
                        if stopped.get('status') in {'completed', 'failed'}:
                            raise RuntimeError('Hermes 在停止请求后结束，请检查任务记录确认已执行的操作')
                        time.sleep(.25)
                    raise RuntimeError('Hermes 已收到停止请求，但尚未确认停止；请检查任务状态')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Hermes 任务超时；请检查任务记录后决定是否继续')
                try:
                    event = stream_events.get(timeout=.1)
                except queue.Empty:
                    event = None
                if event:
                    kind = event.get('event')
                    if kind == 'message.delta':
                        on_event({'type': 'text', 'text': str(event.get('delta', ''))})
                    elif kind == 'tool.started':
                        on_event({'type': 'tool_use', 'name': str(event.get('tool', '工具'))})
                if time.monotonic() >= next_poll:
                    status = request('GET', path)
                    sid = status.get('session_id')
                    if isinstance(sid, str) and sid and sid != seen_session:
                        seen_session = sid
                        on_event({'type': 'system', 'session_id': sid})
                    if status.get('status') in {'completed', 'failed', 'cancelled', 'interrupted'}:
                        break
                    next_poll = time.monotonic() + .5
            if status.get('status') != 'completed':
                raise RuntimeError(str(status.get('error') or f"Hermes 任务{status.get('status')}")[:1500])
            return {'type': 'result', 'exit_code': 0, 'session_id': seen_session,
                    'text': str(status.get('output', ''))}
        finally:
            stop_stream.set()


ADAPTERS = {'hermes': HermesAdapter, 'hermes-gateway': HermesGatewayAdapter}


class AgentTranscript:
    """A composition-compatible sink with NO desktop/clipboard side effects.

    'committed' means accepted into this capture only. Submission to an agent
    occurs once, after successful ASR/postprocessing, never on partial text.
    """
    backend_name = 'agent-buffer'
    supports_segments = True
    preedit_capable = True
    info = 'recordian agent capture'
    stale_reason = ''

    def __init__(self):
        self.active = True
        self.text = ''
        self.preview = ''

    def begin_composition(self, initial_preview=''):
        self.preview = initial_preview
        return self

    def update_preedit(self, text):
        if not self.active:
            return CommitResult(self.backend_name, False, 'capture closed', 'cancelled')
        self.preview = text
        return CommitResult(self.backend_name, True, 'buffered', 'committed')

    def commit_segment(self, text):
        if not self.active:
            return CommitResult(self.backend_name, False, 'capture closed', 'cancelled')
        if len(self.text) + len(text) > MAX_TEXT:
            self.cancel()
            return CommitResult(self.backend_name, False, '语音指令过长', 'cancelled')
        self.text += text
        self.preview = ''
        return CommitResult(self.backend_name, True, 'buffered', 'committed')

    def commit(self, text):
        result = self.commit_segment(text)
        self.active = False
        return result

    def cancel(self):
        self.active = False
        self.preview = ''
        return CommitResult(self.backend_name, False, 'cancelled', 'cancelled')


@dataclass
class Capture:
    mode: str
    agent_id: str
    sink: AgentTranscript = field(default_factory=AgentTranscript)
    delivered: bool = False

    def arguments(self, args):
        copied = argparse.Namespace(**vars(args))
        if self.mode == 'agent':
            copied.enable_streaming_commit = True
            copied.enable_remote_paste = False
            copied.auto_hard_enter = False
            copied._agent_capture = True
        return copied


class AgentHub:
    def __init__(self, config_path: Path, state_path: Path, *, adapters=None, settings_path=None, on_task_event=None):
        self.config_path, self.state_path = config_path, state_path
        self.settings_path = Path(settings_path) if settings_path else config_path.with_name('hotkey.json')
        self.enabled = True
        self.wake_to_agent = True
        raw = json.loads(config_path.read_text())
        self.instances = {a.id: a for a in map(AgentInstance.parse, raw.get('instances', []))}
        if not self.instances:
            raise ValueError('尚未配置 Agent 实例')
        self.selected = raw.get('default_agent') or next(iter(self.instances))
        if self.selected not in self.instances:
            raise ValueError('默认 Agent 不存在')
        self.mode = 'dictation'  # deliberate on every restart; never silently dispatch dictation
        self.lock = threading.RLock()
        self.capture = None
        self.preview = ''
        self.phase = 'idle'
        self.notice = ''
        self.closed = False
        self.cancels = {}
        self.threads = {}
        self.adapters = adapters or ADAPTERS
        self.on_task_event = on_task_event
        self.sessions = {}
        self.tasks = []
        if state_path.exists():
            state = json.loads(state_path.read_text())
            self.sessions = {key: value for key, value in state.get('sessions', {}).items()
                             if state.get('instance_scopes', {}).get(key) == self._scope(key)}
            self.tasks = state.get('tasks', [])[-100:]
            for task in self.tasks:
                if task.get('status') not in TERMINAL:
                    task.update(status='interrupted', error='Recordian 已重启；任务不会自动重发')
        self._save()

    def _save(self):
        private_json(self.state_path, {'sessions': self.sessions, 'tasks': self.tasks[-100:],
                     'instance_scopes': {key: self._scope(key) for key in self.instances}})

    def _scope(self, key):
        instance = self.instances.get(key)
        if instance is None:
            return None
        scope = [instance.executable, instance.workspace, instance.home]
        # Preserve existing CLI session associations across this upgrade.
        return scope if instance.transport == 'cli' else scope + [instance.transport, instance.api_url]

    def _refresh_preferences(self):
        # Read at capture/submission boundaries and when showing settings.
        # A malformed configuration must never enable task execution.
        try:
            data = json.loads(self.settings_path.read_text())
            self.enabled = data.get('enable_agent', True) is True
            self.wake_to_agent = data.get('wake_to_agent', True) is True
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            self.enabled = False

    def set_preferences(self, data):
        if not data or set(data) - {'enable_agent', 'wake_to_agent'} or any(type(v) is not bool for v in data.values()):
            raise ValueError('Agent 设置必须是开关值')
        with self.lock:
            raw = json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {}
            raw.update(data)
            private_json(self.settings_path, raw)
            self._refresh_preferences()
            self.notice = 'Agent 已开启' if self.enabled else 'Agent 已关闭；F9 普通语音输入仍可使用'

    def allows_voice_wake(self):
        with self.lock:
            self._refresh_preferences()
            return self.enabled or not self.wake_to_agent

    def configure_instance(self, data):
        from dataclasses import asdict
        instance = AgentInstance.parse(data)
        with self.lock:
            if self.capture is not None or instance.id in self.cancels:
                raise ValueError('请先结束录音或这个 Agent 的任务')
            if instance.id in self.instances and self.instances[instance.id] != instance:
                self.sessions.pop(instance.id, None)
            self.instances[instance.id] = instance
            private_json(self.config_path, {'default_agent': self.selected,
                         'instances': [asdict(a) for a in self.instances.values()]})
            self._save()

    def snapshot(self):
        from dataclasses import asdict
        with self.lock:
            self._refresh_preferences()
            return json.loads(json.dumps({'mode': self.mode, 'selected': self.selected, 'phase': self.phase,
                'enable_agent': self.enabled, 'wake_to_agent': self.wake_to_agent,
                'preview': self.preview, 'notice': self.notice, 'capturing': self.capture is not None,
                'capture_mode': self.capture.mode if self.capture else None,
                'trigger_routes': {'voice_wake': ('agent' if self.enabled else 'disabled') if self.wake_to_agent else 'dictation',
                                   'hotkey': 'dictation', 'agent_hotkey': 'agent' if self.enabled else 'disabled',
                                   'agent_panel': 'agent' if self.enabled else 'disabled'},
                'instances': [asdict(a) for a in self.instances.values()], 'sessions': self.sessions,
                'tasks': self.tasks[-100:]}, ensure_ascii=False))

    def select(self, mode, agent_id):
        if mode not in {'agent', 'dictation'} or agent_id not in self.instances:
            raise ValueError('未知模式或 Agent')
        with self.lock:
            if self.capture is not None:
                raise ValueError('请先结束当前录音并等待识别完成')
            self.mode, self.selected = mode, agent_id
            self.preview = ''
            self.notice = '语音将交给 ' + self.instances[agent_id].name if mode == 'agent' else '语音将输入当前应用'

    def begin_capture(self, trigger_source='panel'):
        with self.lock:
            self._refresh_preferences()
            if self.closed or self.capture is not None:
                raise RuntimeError('语音入口正在处理上一段录音')
            modes = {'voice_wake': 'agent' if self.wake_to_agent else 'dictation',
                     'agent_panel': 'agent', 'agent_hotkey': 'agent',
                     'hotkey': 'dictation', 'panel': self.mode}
            if trigger_source not in modes:
                raise ValueError('未知录音来源')
            mode = modes[trigger_source]
            if mode == 'agent' and not self.enabled:
                raise RuntimeError('Agent 已关闭，F9 普通语音输入仍可使用')
            if mode == 'agent' and self.selected in self.cancels:
                raise RuntimeError('这个 Agent 正在执行任务，请等它完成或先停止任务')
            self.capture = Capture(mode, self.selected)
            self.preview = ''
            return self.capture

    def end_capture(self, capture):
        with self.lock:
            if self.capture is capture:
                self.capture = None
                self.phase = 'idle'

    def observe(self, payload):
        with self.lock:
            event = payload.get('event')
            if event in {'realtime_asr_partial', 'stream_partial'}:
                self.preview = str(payload.get('text', ''))[-MAX_TEXT:]
            elif event in {'recording_started', 'processing_started'}:
                self.phase = 'recording' if event == 'recording_started' else 'transcribing'
            elif event == 'error':
                self.notice = str(payload.get('error', ''))[:2000]

    def accept_transcript(self, capture, payload, *, failed=False):
        if capture is None or capture.mode != 'agent':
            return
        with self.lock:
            if capture.delivered or self.capture is not capture:
                return
            capture.delivered = True
            result = payload.get('result', {})
            self.preview = str(result.get('text', ''))
            self._refresh_preferences()
            if not self.enabled:
                self.notice = 'Agent 已关闭，这段录音没有发送；可在面板查看识别文字'
                return
            outcome = (result.get('commit') or {}).get('outcome', '')
            if failed or outcome in {'uncertain', 'stale', 'cancelled', 'suppressed'}:
                self.notice = '这段录音未完整识别，没有提交给 Agent，请重新录音'
                return
            if not self.preview.strip():
                self.notice = '没有识别到指令'
                return
            self.submit(self.preview, capture.agent_id)

    def new_session(self, agent_id):
        with self.lock:
            if self.capture is not None or agent_id in self.cancels:
                raise ValueError('请先结束录音或正在执行的任务')
            self.sessions.pop(agent_id, None)
            self._save()

    def submit(self, text, agent_id, request_id=None):
        text = str(text).strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError('请输入有效指令（不超过 60000 字符）')
        with self.lock:
            self._refresh_preferences()
            if not self.enabled:
                raise ValueError('Agent 已关闭，请先在设置中开启')
            if self.closed:
                raise ValueError('语音入口已停止')
            if request_id:
                old = next((t for t in self.tasks if t.get('request_id') == request_id), None)
                if old:
                    if old['agent_id'] != agent_id or old['prompt'] != text:
                        raise ValueError('重复请求编号对应了不同指令')
                    return old['id']
            if agent_id not in self.instances or agent_id in self.cancels:
                raise ValueError('Agent 不存在或仍在执行上一项任务')
            task = {'id': uuid.uuid4().hex, 'agent_id': agent_id, 'prompt': text, 'status': 'running',
                    'reply': '', 'error': '', 'activity': '正在连接 Hermes', 'created': time.time(),
                    'session_id': self.sessions.get(agent_id, ''), 'request_id': request_id or uuid.uuid4().hex}
            cancel = threading.Event()
            self.tasks.append(task)
            # Only prune terminal tasks; active tasks must remain visible.
            while len(self.tasks) > 100:
                index = next((i for i, t in enumerate(self.tasks) if t['status'] in TERMINAL), None)
                if index is None:
                    break
                self.tasks.pop(index)
            self.cancels[agent_id] = cancel
            self._save()
            thread = threading.Thread(target=self._run, args=(task, cancel), daemon=True, name='recordian-agent-'+agent_id)
            self.threads[agent_id] = thread
            thread.start()
            return task['id']

    def _run(self, task, cancel):
        agent_id = task['agent_id']
        instance = self.instances[agent_id]
        self._emit_task(task, 'started')
        last_stream_emit = 0.0

        def update(event):
            nonlocal last_stream_emit
            emit_type = ''
            with self.lock:
                kind = event.get('type')
                if kind == 'text':
                    task['reply'] = (task['reply'] + str(event.get('text', '')))[:MAX_TEXT]
                    now = time.monotonic()
                    if now - last_stream_emit >= .08:
                        last_stream_emit = now
                        emit_type = 'stream'
                elif kind == 'tool_use':
                    task['activity'] = '正在使用：' + str(event.get('name', '工具'))[:100]
                    emit_type = 'activity'
                elif kind in {'system', 'result'} and event.get('session_id'):
                    task['session_id'] = str(event['session_id'])
                    self.sessions[agent_id] = task['session_id']
                    self._save()
            if emit_type:
                self._emit_task(task, emit_type)
        try:
            if instance.transport == 'gateway':
                result = self.adapters['hermes-gateway']().run(instance, task['prompt'], task['session_id'], cancel, update,
                                                               request_id=task['id'])
            else:
                result = self.adapters[instance.kind]().run(instance, task['prompt'], task['session_id'], cancel, update)
            with self.lock:
                task.update(status='completed', reply=str(result.get('text', ''))[:MAX_TEXT], activity='已完成')
        except InterruptedError as exc:
            with self.lock:
                task.update(status='cancelled', error=str(exc), activity='已停止')
        except Exception as exc:
            with self.lock:
                task.update(status='failed', error=str(exc)[:2000], activity='需要处理')
        finally:
            with self.lock:
                self.cancels.pop(agent_id, None)
                task['finished'] = time.time()
                self._save()
            self._emit_task(task, 'finished')

    def _emit_task(self, task, event_type):
        if self.on_task_event is None:
            return
        with self.lock:
            snapshot = dict(task)
            snapshot['event_type'] = event_type
            agent_name = self.instances[task['agent_id']].name
        try:
            self.on_task_event(snapshot, agent_name)
        except Exception:
            # Notification failures must not change task delivery or execution.
            pass

    def cancel(self, agent_id):
        with self.lock:
            event = self.cancels.get(agent_id)
            if event:
                event.set()

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancels.values():
                event.set()
            threads = list(self.threads.values())
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0, deadline-time.monotonic()))
