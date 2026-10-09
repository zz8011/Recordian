"""Regression checks for network and private-file boundaries found in the audit."""
from __future__ import annotations

import argparse
import base64
from types import SimpleNamespace

import pytest

from recordian.agent_entry import private_json
from recordian.local_auth import load_private_token, require_private_bind
from recordian.remote_paste.agent import RemotePasteAgent, RemotePasteRequestHandler, RemotePasteTCPServer
from recordian.remote_paste.client import _read_response_line
from recordian.remote_paste.protocol import MAX_MESSAGE_BYTES
from server import asr_server


def agent_args(**overrides):
    return argparse.Namespace(**dict(hostname='test', enable_notify=False, notify_backend='none',
                                    paste_delay_ms=0, commit_backend='none', **overrides))


def test_remote_paste_rejects_missing_or_incorrect_token_before_commit(tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('test-only-secret')
    token.chmod(0o600)
    app = RemotePasteAgent(agent_args(token_file=str(token)))
    commits = []
    monkeypatch.setattr(app, '_handle_paste', lambda payload: commits.append(payload) or {'status': 'ok'})
    for supplied in (None, 'wrong', '\u6d4b\u8bd5'):
        response = app.handle_payload({'action': 'paste', 'text': 'synthetic', 'token': supplied})
        assert response['detail'] == 'unauthorized'
    assert commits == []
    assert app.handle_payload({'action': 'paste', 'token': 'test-only-secret'})['status'] == 'ok'
    assert len(commits) == 1


def test_remote_bind_without_auth_is_rejected():
    with pytest.raises(ValueError, match='token'):
        RemotePasteTCPServer(('0.0.0.0', 0), RemotePasteRequestHandler, RemotePasteAgent(agent_args()))


def test_newline_does_not_bypass_response_limit():
    class FakeSocket:
        def recv(self, _):
            return b'x' * (MAX_MESSAGE_BYTES + 1) + b'\n'
    with pytest.raises(RuntimeError, match='response_too_large'):
        _read_response_line(FakeSocket())


@pytest.mark.parametrize('wait', [float('inf'), float('nan'), 99999, -1])
def test_paste_only_rejects_unbounded_clipboard_wait(wait, monkeypatch):
    def forbidden_read():
        raise AssertionError('invalid request reached desktop clipboard')
    monkeypatch.setattr('recordian.remote_paste.agent._get_clipboard_text', forbidden_read)
    app = RemotePasteAgent(agent_args())
    response = app.handle_payload({'action': 'paste_only', 'expected_text': 'synthetic', 'clipboard_wait_s': wait})
    assert response['detail'] == 'invalid_clipboard_wait'


def test_private_json_does_not_follow_predictable_temp_symlink(tmp_path):
    target = tmp_path / 'state.json'
    victim = tmp_path / 'victim'
    victim.write_text('preserve')
    target.with_name(target.name + '.tmp').symlink_to(victim)
    private_json(target, {'state': 'synthetic'})
    assert victim.read_text() == 'preserve'
    assert target.stat().st_mode & 0o777 == 0o600


def test_asr_rejects_nonobject_json_and_invalid_base64(monkeypatch):
    calls = []
    monkeypatch.setattr(asr_server, 'asr_model', SimpleNamespace(transcribe=lambda **kw: calls.append(kw)))
    client = asr_server.app.test_client()
    assert client.post('/transcribe', json=['audio']).status_code == 400
    assert client.post('/transcribe', json={'audio_base64': '!!!!'}).status_code == 400
    assert calls == []


def test_asr_rejects_generation_budget_escalation(monkeypatch):
    calls = []
    model = SimpleNamespace(max_new_tokens=64, transcribe=lambda **kw: calls.append(kw) or [SimpleNamespace(text='synthetic')])
    monkeypatch.setattr(asr_server, 'asr_model', model)
    client = asr_server.app.test_client()
    response = client.post('/transcribe', json={'audio_base64': base64.b64encode(b'RIFFdemo').decode(),
                                               'max_new_tokens': 1000000000})
    assert response.status_code == 400
    assert calls == []
    assert model.max_new_tokens == 64


@pytest.mark.parametrize('value', ['first\nsecond', 'first\rsecond', 'token ' + ' ' * 4096 + 'tail', '\u6d4b\u8bd5'])
def test_token_file_rejects_non_header_safe_or_oversize_content(tmp_path, value):
    path = tmp_path / 'token'
    path.write_text(value)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        load_private_token(str(path))


def test_lan_bind_requires_encrypted_transport_even_with_token():
    with pytest.raises(ValueError, match='TLS'):
        require_private_bind('0.0.0.0', 'test-only-secret')


def test_bridge_preserves_413_status():
    from server import confucius_openai_bridge as bridge
    client = bridge.app.test_client()
    response = client.post('/v1/audio/transcriptions', data=b'x',
        content_type='multipart/form-data; boundary=test', environ_overrides={'CONTENT_LENGTH': str(17 * 1024 * 1024)})
    assert response.status_code == 413
