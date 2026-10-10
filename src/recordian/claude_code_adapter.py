"""Claude Code print/JSON adapter; no CLI invocation during discovery.

Protocol: https://code.claude.com/docs/en/headless and /cli-reference.
One explicitly submitted turn per process, no automatic retry or --continue.
The CLI retains its configured permissions; this adapter never bypasses them.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid

MAX_OUTPUT = 1024 * 1024


class ClaudeCodeAdapter:
    @staticmethod
    def command(instance, session_id):
        argv = [instance.executable, '--print', '--output-format', 'json']
        if session_id:
            # Resume an exact receipt, never a name, path or "latest" picker.
            try:
                normalized = str(uuid.UUID(session_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError('Claude Code 会话编号无效，请开始新会话；本次未提交') from exc
            if normalized != session_id.lower():
                raise ValueError('Claude Code 会话编号不是完整 UUID；本次未提交')
            argv += ['--resume', normalized]
        return argv

    def run(self, instance, text, session_id, cancel, on_event):
        if cancel.is_set():
            raise InterruptedError('任务已停止；本次未提交')
        argv = self.command(instance, session_id)
        proc = subprocess.Popen(argv, cwd=instance.workspace, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        output = bytearray()
        faults = []

        def read_output():
            try:
                while chunk := proc.stdout.read(4096):
                    if len(output) + len(chunk) > MAX_OUTPUT:
                        faults.append('Claude Code 结果超过 1 MiB，本次不会自动重试')
                        return
                    output.extend(chunk)
            except (OSError, ValueError):
                faults.append('Claude Code 输出读取失败，本次不会自动重试')

        def drain_error():
            # Drain without persisting stderr, which may contain host details.
            try:
                while proc.stderr.read(4096):
                    pass
            except (OSError, ValueError):
                pass

        def write_prompt():
            try:
                proc.stdin.write(text.encode('utf-8'))
                proc.stdin.close()
            except (OSError, ValueError):
                faults.append('Claude Code 未完整接收指令，本次不会自动重试')

        workers = [threading.Thread(target=fn, daemon=True) for fn in (read_output, drain_error, write_prompt)]
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + instance.timeout_s
        try:
            while proc.poll() is None or any(w.is_alive() for w in workers):
                if cancel.wait(.05):
                    raise InterruptedError('任务已停止；已经执行的操作不会自动撤销')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Claude Code 执行超时，请检查任务记录；不会自动重试')
                if faults:
                    raise RuntimeError(faults[0])
            if cancel.is_set():
                raise InterruptedError('任务已停止；请核对是否已执行操作')
            if faults:
                raise RuntimeError(faults[0])
            code = proc.wait(timeout=2)
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError(f'Claude Code 未返回有效 JSON（退出码 {code}）；请核对 CLI 登录、版本或配置') from exc
            if not isinstance(result, dict) or result.get('type') != 'result':
                raise RuntimeError('Claude Code 未返回最终结果；不会自动重试')
            if code != 0 or result.get('subtype') != 'success' or result.get('is_error', False) is not False:
                errors = result.get('errors', [])
                detail = '; '.join(e for e in errors if isinstance(e, str)) if isinstance(errors, list) else ''
                raise RuntimeError(f'Claude Code 任务未完成（退出码 {code}）：' + (detail[:1500] or '请核对认证、权限或任务限制'))
            if result.get('permission_denials'):
                raise RuntimeError('Claude Code 有工具权限未获授权，请在其终端核对权限；本次不会自动重试')
            reply = result.get('result')
            sid = result.get('session_id')
            if not isinstance(reply, str) or not isinstance(sid, str):
                raise RuntimeError('Claude Code 最终结果缺少文本或会话编号；不会自动重试')
            self.command(instance, sid)  # Validate the returned ID before storing it.
            terminal = {'type': 'result', 'exit_code': 0, 'session_id': sid.lower(), 'text': reply}
            on_event(terminal)
            return terminal
        finally:
            if proc.poll() is None or any(w.is_alive() for w in workers):
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        pass
                    for worker in workers:
                        worker.join(timeout=.2)
                    if proc.poll() is not None and not any(w.is_alive() for w in workers):
                        break
            proc.wait(timeout=3)
            for worker in workers:
                worker.join(timeout=.2)
            if not any(w.is_alive() for w in workers):
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if not stream.closed:
                        stream.close()
