"""Loopback-only task panel with a per-process capability token.

The token travels in an HTTP header (URL fragment at initial browser launch),
never in query strings. No permissive CORS or unauthenticated mutations.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent_entry import MAX_TEXT, private_json


def address_path():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'recordian' / 'agent-panel.json'


def open_panel():
    state = json.loads(address_path().read_text())
    webbrowser.open(state['url'] + '/#' + state['token'])


class AgentPanel:
    def __init__(self, hub, *, path=None, record=None):
        self.hub = hub
        self.path = path or address_path()
        self.token = secrets.token_urlsafe(32)
        if record is None:
            from .desktop_control import send_action
            def record():
                return send_action('toggle')
        self.record = record
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data, content_type='application/json; charset=utf-8'):
                body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                valid = hmac.compare_digest(self.headers.get('X-Recordian-Token', ''), owner.token)
                origin = self.headers.get('Origin')
                if self.headers.get('Host') != owner.url.removeprefix('http://') or (origin and origin != owner.url):
                    valid = False
                if not valid:
                    self.respond(403, {'error': '请从 Recordian 托盘重新打开任务面板'})
                return valid

            def do_GET(self):
                if self.path == '/':
                    self.respond(200, Path(__file__).with_name('agent_panel.html').read_bytes(), 'text/html; charset=utf-8')
                elif self.path == '/api/state':
                    if self.authorized():
                        self.respond(200, owner.hub.snapshot())
                else:
                    self.respond(404, {'error': '不存在'})

            def do_POST(self):
                if not self.authorized():
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if size <= 0 or size > MAX_TEXT * 6:
                        raise ValueError('请求长度无效')
                    self.connection.settimeout(3)
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError('请求无效')
                    if self.path == '/api/select':
                        owner.hub.select(data['mode'], data['agent_id'])
                    elif self.path == '/api/submit':
                        with owner.hub.lock:
                            if owner.hub.capture is not None:
                                raise ValueError('请等当前录音处理完成')
                            owner.hub.submit(data['text'], data['agent_id'], data.get('request_id'))
                    elif self.path == '/api/cancel':
                        owner.hub.cancel(data['agent_id'])
                    elif self.path == '/api/new-session':
                        owner.hub.new_session(data['agent_id'])
                    elif self.path == '/api/instance':
                        owner.hub.configure_instance(data)
                    elif self.path == '/api/record':
                        with owner.hub.lock:
                            if owner.hub.mode != 'agent':
                                raise ValueError('请先切换到语音指令模式')
                        owner.record()
                    else:
                        self.respond(404, {'error': '不存在'})
                        return
                    self.respond(200, {'ok': True})
                except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                    self.respond(400, {'error': str(exc)[:1500]})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='recordian-agent-panel')
        self.thread.start()
        private_json(self.path, {'url': self.url, 'token': self.token, 'pid': os.getpid()})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        try:
            if json.loads(self.path.read_text()).get('token') == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass


def main():
    parser = argparse.ArgumentParser(description='Recordian Agent 任务面板')
    parser.add_argument('action', choices=['open', 'status'], nargs='?', default='open')
    args = parser.parse_args()
    if args.action == 'open':
        open_panel()
    else:
        import urllib.request
        state = json.loads(address_path().read_text())
        req = urllib.request.Request(state['url'] + '/api/state', headers={'X-Recordian-Token': state['token']})
        with urllib.request.urlopen(req, timeout=3) as response:
            print(response.read().decode())


if __name__ == '__main__':
    main()
