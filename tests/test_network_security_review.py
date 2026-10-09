"""Security regressions using ephemeral loopback sockets and fake desktop/models.

No installed service, GPU model, real clipboard, or desktop is contacted.
Certificates, tokens and audio fixtures exist only in pytest temporary directories.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import socket
import ssl
import subprocess
import sys
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from flask import Flask, jsonify, request

from recordian import audio_budget, http_service, local_auth
from recordian.remote_paste import agent as paste_agent
from recordian.remote_paste import client as paste_client
from recordian.remote_paste.protocol import decode_message, encode_message
from server import asr_server
from server import confucius_openai_bridge as bridge
from server import qwen_streaming_server as qwen_server

TOKEN = "synthetic-network-review-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _token_file(directory, content=TOKEN, mode=0o600):
    path = directory / "token"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    """Trust one certificate; a separately generated certificate is untrusted."""
    directory = tmp_path_factory.mktemp("network-review-certificates")
    pairs = []
    for name in ("trusted", "untrusted"):
        cert, key = directory / f"{name}.crt", directory / f"{name}.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=network-review",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        pairs.append((cert, key))
    return pairs


@pytest.fixture
def desktop(monkeypatch):
    """Replace only the operations that would touch the user's desktop."""
    effects = []

    class Committer:
        backend_name = "review-fake"

        def commit(self, text):
            effects.append(("commit", text))
            return SimpleNamespace(committed=True, detail="committed")

    def focus():
        effects.append(("focus",))
        return 123

    def clipboard():
        effects.append(("clipboard",))
        return "synthetic-private-clipboard"

    def shortcut(**kwargs):
        effects.append(("shortcut",))
        return SimpleNamespace(committed=True, detail="pasted")

    monkeypatch.setattr(paste_agent, "get_focused_window_id", focus)
    monkeypatch.setattr(paste_agent, "resolve_committer", lambda *args, **kwargs: Committer())
    monkeypatch.setattr(paste_agent, "_get_clipboard_text", clipboard)
    monkeypatch.setattr(paste_agent, "send_paste_shortcut", shortcut)
    monkeypatch.setattr(paste_client, "_set_clipboard_text", lambda text: effects.append(("stage", text)))
    return effects


@contextmanager
def _paste_server(tmp_path, *, authenticated=False, certificate=None):
    argv = ["--no-notify", "--paste-delay-ms", "0", "--commit-backend", "none", "--hostname", "review-fake-desktop"]
    if authenticated:
        argv += ["--token-file", str(_token_file(tmp_path))]
    if certificate:
        argv += ["--tls-cert-file", str(certificate[0]), "--tls-key-file", str(certificate[1])]
    app = paste_agent.RemotePasteAgent(paste_agent.build_parser().parse_args(argv))
    with paste_agent.RemotePasteTCPServer(
        ("127.0.0.1", 0),
        paste_agent.RemotePasteRequestHandler,
        app,
    ) as server:
        # Failed TLS handshakes are expected negative inputs. Collect handler errors
        # locally instead of printing socketserver's traceback to the user's logs.
        errors = []
        server.handle_error = lambda request, address: errors.append(address)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive(), "ephemeral test server did not shut down"


def _socket_command(address, payload):
    with socket.create_connection(address, timeout=2) as sock:
        sock.sendall(encode_message(payload))
        return decode_message(paste_client._read_response_line(sock))


def test_loopback_plaintext_without_token_preserves_old_transport(tmp_path, desktop):
    with _paste_server(tmp_path) as server:
        response = _socket_command(server.server_address, {"action": "paste", "text": "synthetic-text"})
    assert response["status"] == "ok"
    assert desktop == [("focus",), ("commit", "synthetic-text")]


@pytest.mark.parametrize("supplied", ["", "incorrect", "非ASCII"])
def test_tls_wrong_token_cannot_reach_desktop(tmp_path, certificates, desktop, supplied):
    cert, _key = certificates[0]
    with _paste_server(tmp_path, authenticated=True, certificate=certificates[0]) as server:
        response = paste_client.send_remote_paste(
            "127.0.0.1",
            "synthetic-private-text",
            port=server.server_address[1],
            timeout_s=2,
            token=supplied,
            tls_ca_file=str(cert),
        )
    assert not response.ok
    assert response.detail == "unauthorized"
    assert desktop == []


def test_verified_tls_correct_token_reaches_fake_committer(tmp_path, certificates, desktop):
    cert, _key = certificates[0]
    with _paste_server(tmp_path, authenticated=True, certificate=certificates[0]) as server:
        response = paste_client.send_remote_paste(
            "127.0.0.1",
            "synthetic-text",
            port=server.server_address[1],
            timeout_s=2,
            token=TOKEN,
            tls_ca_file=str(cert),
        )
    assert response.ok
    assert desktop == [("focus",), ("commit", "synthetic-text")]


@pytest.mark.parametrize("mismatch", ["trust", "hostname"])
def test_tls_certificate_validation_fails_before_desktop(tmp_path, certificates, desktop, mismatch):
    trust = certificates[1][0] if mismatch == "trust" else certificates[0][0]
    # The trusted certificate contains only IP:127.0.0.1, not DNS:localhost.
    host = "127.0.0.1" if mismatch == "trust" else "localhost"
    with _paste_server(tmp_path, authenticated=True, certificate=certificates[0]) as server:
        with pytest.raises(ssl.SSLCertVerificationError):
            paste_client.send_remote_paste(
                host,
                "synthetic-text",
                port=server.server_address[1],
                timeout_s=2,
                token=TOKEN,
                tls_ca_file=str(trust),
            )
    assert desktop == []


def test_direct_lan_token_without_tls_is_rejected_before_connect(monkeypatch):
    def forbidden_connect(*args, **kwargs):
        pytest.fail("unsafe LAN request attempted a connection")

    monkeypatch.setattr(paste_client.socket, "create_connection", forbidden_connect)
    with pytest.raises(ValueError, match="TLS"):
        paste_client.send_remote_paste("192.0.2.1", "synthetic", port=24872, timeout_s=1, token=TOKEN)


@pytest.mark.parametrize(("token", "encrypted"), [("", False), ("", True), (TOKEN, False)])
def test_lan_bind_requires_both_token_and_encryption(token, encrypted):
    with pytest.raises(ValueError):
        local_auth.require_private_bind("0.0.0.0", token, encrypted=encrypted)


def test_slow_drips_hit_absolute_deadline_and_release_all_slots(tmp_path, desktop):
    sockets = []
    stop = threading.Event()
    with _paste_server(tmp_path, authenticated=True) as server:
        started = time.monotonic()
        try:
            for _ in range(8):
                sock = socket.create_connection(server.server_address, timeout=5)
                sockets.append(sock)
                sock.sendall(b"{")

            def trickle():
                while not stop.wait(0.1):
                    for connection in sockets:
                        try:
                            connection.sendall(b" ")
                        except OSError:
                            pass

            dripper = threading.Thread(target=trickle)
            dripper.start()
            try:
                replies = [_read_to_close(sock) for sock in sockets]
            finally:
                stop.set()
                dripper.join(timeout=2)
            assert time.monotonic() - started < 4.8, "idle timeout replaced the absolute frame deadline"
            # Receive/handshake timeout now closes without attempting an error write.
            assert replies == [b""] * 8
            # A new request must succeed after expired connections release admission slots.
            deadline = time.monotonic() + 1
            while True:
                try:
                    response = _socket_command(server.server_address, {"action": "ping", "token": TOKEN})
                    break
                except (OSError, RuntimeError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            assert response["status"] == "pong"
        finally:
            stop.set()
            for sock in sockets:
                sock.close()
    assert desktop == []


def test_stalled_tls_handshake_does_not_hold_a_slot_past_deadline(tmp_path, certificates, desktop):
    with _paste_server(tmp_path, authenticated=True, certificate=certificates[0]) as server:
        with socket.create_connection(server.server_address, timeout=7) as raw:
            started = time.monotonic()
            assert raw.recv(1) == b""
            elapsed = time.monotonic() - started
            assert elapsed < 4.8, f"TLS handshake held admission slot for {elapsed:.2f}s"
        response = paste_client.send_remote_paste(
            "127.0.0.1",
            "synthetic",
            port=server.server_address[1],
            timeout_s=2,
            token=TOKEN,
            tls_ca_file=str(certificates[0][0]),
        )
        assert response.ok


def test_token_symlink_rejected_without_reading_target(tmp_path):
    target = _token_file(tmp_path)
    link = tmp_path / "token-link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        local_auth.load_private_token(str(link))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604])
def test_token_with_group_or_other_access_is_rejected(tmp_path, mode):
    with pytest.raises(ValueError):
        local_auth.load_private_token(str(_token_file(tmp_path, mode=mode)))


@pytest.mark.parametrize(
    "content",
    [
        "",
        " " * 64,
        "x" * 4097,
        TOKEN + "\n" + " " * 4097 + "tail",
        "first\nsecond",
        "first\rsecond",
        "first\tsecond",
        "first\x00second",
        "first\x7fsecond",
        "非ASCII",
    ],
)
def test_token_content_rejects_oversize_or_invalid_header_characters(tmp_path, content):
    with pytest.raises(ValueError):
        local_auth.load_private_token(str(_token_file(tmp_path, content)))


def test_token_exact_size_boundary_and_single_trailing_newline(tmp_path):
    path = _token_file(tmp_path, "x" * 4096)
    assert local_auth.load_private_token(str(path)) == "x" * 4096
    path.write_text(TOKEN + "\n", encoding="utf-8")
    assert local_auth.load_private_token(str(path)) == TOKEN


def _wav_bytes(*, seconds=0.1, rate=16000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buffer.getvalue()


def _audio_payload(**extra):
    return {"audio_base64": base64.b64encode(_wav_bytes()).decode("ascii"), **extra}


@pytest.fixture
def model(monkeypatch):
    calls = []

    class Model:
        max_new_tokens = 64

        def transcribe(self, **kwargs):
            calls.append(self.max_new_tokens)
            assert Path(kwargs["audio"]).is_file()
            return [SimpleNamespace(text="synthetic-result")]

    instance = Model()
    monkeypatch.setattr(asr_server, "asr_model", instance)
    monkeypatch.setattr(asr_server, "auth_token", TOKEN)
    monkeypatch.setattr(asr_server, "model_name", "review-fake-model")
    return instance, calls


@pytest.mark.parametrize("authorization", ["", "Bearer wrong"])
def test_asr_auth_rejects_before_model_and_body_processing(model, authorization):
    instance, calls = model
    response = asr_server.app.test_client().post(
        "/transcribe",
        data=b"",
        content_type="application/json",
        headers={"Authorization": authorization},
        environ_overrides={"CONTENT_LENGTH": str(16 * 1024 * 1024 + 1)},
    )
    assert response.status_code == 401
    assert calls == []
    assert instance.max_new_tokens == 64


def test_asr_request_cannot_exceed_model_budget_below_global_cap(model):
    instance, calls = model
    response = asr_server.app.test_client().post("/transcribe", json=_audio_payload(max_new_tokens=65), headers=HEADERS)
    assert response.status_code == 400
    assert calls == []
    assert instance.max_new_tokens == 64


def test_asr_success_restores_budget_and_reports_request_setting(model):
    instance, calls = model
    response = asr_server.app.test_client().post("/transcribe", json=_audio_payload(max_new_tokens=7), headers=HEADERS)
    assert response.status_code == 200
    assert response.get_json()["max_new_tokens"] == 7
    assert calls == [7]
    assert instance.max_new_tokens == 64


def test_asr_model_failure_restores_budget_for_next_request(model, monkeypatch):
    instance, calls = model
    original = instance.transcribe

    def fail(**kwargs):
        calls.append(instance.max_new_tokens)
        raise RuntimeError("synthetic-model-failure")

    monkeypatch.setattr(instance, "transcribe", fail)
    response = asr_server.app.test_client().post("/transcribe", json=_audio_payload(max_new_tokens=7), headers=HEADERS)
    assert response.status_code == 500
    assert instance.max_new_tokens == 64
    monkeypatch.setattr(instance, "transcribe", original)
    response = asr_server.app.test_client().post("/transcribe", json=_audio_payload(), headers=HEADERS)
    assert response.status_code == 200
    assert calls == [7, 64]


def test_concurrent_asr_request_is_rejected_and_later_request_observes_restored_budget(model, monkeypatch):
    instance, calls = model
    first_entered = threading.Event()
    release_first = threading.Event()

    def transcribe(**kwargs):
        before = instance.max_new_tokens
        calls.append(before)
        if before == 7:
            first_entered.set()
            assert release_first.wait(3), "test did not release the first inference"
        assert instance.max_new_tokens == before, "another request mutated an active inference"
        return [SimpleNamespace(text="synthetic-result")]

    monkeypatch.setattr(instance, "transcribe", transcribe)
    replies, errors = {}, []

    def request(name, payload):
        try:
            replies[name] = asr_server.app.test_client().post("/transcribe", json=payload, headers=HEADERS)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=request, args=("first", _audio_payload(max_new_tokens=7)), name="review-asr-first")
    second = threading.Thread(target=request, args=("second", _audio_payload()), name="review-asr-second")
    first.start()
    try:
        assert first_entered.wait(3)
        second.start()
        second.join(timeout=1)
        assert not second.is_alive(), "busy request waited instead of failing admission"
        assert replies["second"].status_code == 429
        assert calls == [7]
    finally:
        release_first.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert replies["first"].status_code == 200
    assert replies["first"].get_json()["max_new_tokens"] == 7
    later = asr_server.app.test_client().post("/transcribe", json=_audio_payload(), headers=HEADERS)
    assert later.status_code == 200
    assert later.get_json()["max_new_tokens"] == 64
    assert calls == [7, 64]
    assert instance.max_new_tokens == 64


@pytest.mark.parametrize("seconds", [120.001, 121])
def test_legacy_http_rejects_over_duration_wav_before_model(model, seconds):
    instance, calls = model
    response = asr_server.app.test_client().post(
        "/transcribe",
        json={"audio_base64": base64.b64encode(_wav_bytes(seconds=seconds)).decode("ascii")},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert calls == []
    assert instance.max_new_tokens == 64


def test_wav_sample_limit_is_independent_of_duration_limit():
    # 84s * 192000Hz is over 16M samples, yet below the 120s limit.
    with pytest.raises(ValueError, match="budget"):
        audio_budget.validate_wav(_wav_bytes(seconds=84, rate=192000, width=1))


def test_legacy_http_truncated_wav_rejected_before_model(model):
    _instance, calls = model
    truncated = _wav_bytes()[:-20]
    response = asr_server.app.test_client().post(
        "/transcribe",
        json={"audio_base64": base64.b64encode(truncated).decode("ascii")},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert calls == []


@pytest.fixture
def qwen_app(tmp_path, monkeypatch, request):
    """Run real route registration, replacing only the absent upstream/model runtime."""
    app = Flask("network-review-qwen")
    calls = []
    sessions = {}
    gc_calls = []

    class Model:
        def init_streaming_state(self, **kwargs):
            calls.append(("start", kwargs))
            return SimpleNamespace(text="", language="Chinese")

        def streaming_transcribe(self, samples, state):
            calls.append(("chunk", samples.size))
            state.text = "synthetic-chunk"

        def transcribe(self, **kwargs):
            with wave.open(kwargs["audio"], "rb") as wav:
                assert wav.getframerate() == 16000
                assert wav.getnchannels() == 1
                assert wav.getsampwidth() == 2
            calls.append(("upload", kwargs))
            return [SimpleNamespace(text="synthetic-result", language="Chinese")]

    def gc_sessions():
        gc_calls.append(True)
        now = time.time()
        for identifier, session in list(sessions.items()):
            if now - session.last_seen > 600:
                sessions.pop(identifier)

    upstream = ModuleType("qwen_asr.cli.demo_streaming")
    upstream.app, upstream.SESSIONS = app, sessions
    upstream.Session = SimpleNamespace
    upstream._gc_sessions = gc_sessions
    upstream._get_session = sessions.get
    app.add_url_rule("/api/start", "api_start", lambda: jsonify({}), methods=["POST"])
    app.add_url_rule("/api/chunk", "api_chunk", lambda: jsonify({}), methods=["POST"])
    package = ModuleType("qwen_asr")
    cli = ModuleType("qwen_asr.cli")
    package.cli, cli.demo_streaming = cli, upstream
    package.Qwen3ASRModel = SimpleNamespace(LLM=lambda **kwargs: Model())
    for name, module in [("qwen_asr", package), ("qwen_asr.cli", cli), ("qwen_asr.cli.demo_streaming", upstream)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(qwen_server, "demo", None)
    monkeypatch.setattr(http_service, "run_http_service", lambda *args, **kwargs: None)
    argv = ["--token-file", str(_token_file(tmp_path))] if getattr(request, "param", True) else []
    qwen_server.main(argv)
    return app, upstream, calls, gc_calls


def test_qwen_auth_guards_stream_and_upload_before_model(qwen_app):
    app, _upstream, calls, _gc = qwen_app
    client = app.test_client()
    assert client.post("/api/start", json={}).status_code == 401
    assert (
        client.post("/v1/audio/transcriptions", data={"file": (io.BytesIO(_wav_bytes()), "audio.wav")}).status_code
        == 401
    )
    assert calls == []


def test_qwen_session_limit_rejects_ninth_then_runs_gc_before_admission(qwen_app):
    app, upstream, calls, gc_calls = qwen_app
    client = app.test_client()
    for _ in range(8):
        assert client.post("/api/start", json={}, headers=HEADERS).status_code == 200
    assert client.post("/api/start", json={}, headers=HEADERS).status_code == 429
    assert len(upstream.SESSIONS) == 8
    assert len(calls) == 8
    for session in upstream.SESSIONS.values():
        session.last_seen = time.time() - 601
    assert client.post("/api/start", json={}, headers=HEADERS).status_code == 200
    assert len(upstream.SESSIONS) == 1
    assert len(gc_calls) == 10


@pytest.mark.parametrize("body", [[], [1]])
def test_qwen_nonobject_start_json_rejected_before_model(qwen_app, body):
    app, upstream, calls, _gc = qwen_app
    response = app.test_client().post("/api/start", json=body, headers=HEADERS)
    assert response.status_code == 400
    assert calls == []
    assert upstream.SESSIONS == {}


@pytest.mark.parametrize("raw", [b"\x00", b"\x00\x00\xc0\x7f", b"\x00\x00\x80\x7f"])
def test_qwen_invalid_chunk_never_reaches_model(qwen_app, raw):
    app, _upstream, calls, _gc = qwen_app
    client = app.test_client()
    identifier = client.post("/api/start", json={}, headers=HEADERS).get_json()["session_id"]
    response = client.post(
        f"/api/chunk?session_id={identifier}", data=raw, content_type="application/octet-stream", headers=HEADERS
    )
    assert response.status_code == 400
    assert [name for name, _ in calls] == ["start"]


def test_qwen_chunk_accumulated_samples_bounded_before_model(qwen_app):
    app, upstream, calls, _gc = qwen_app
    client = app.test_client()
    identifier = client.post("/api/start", json={}, headers=HEADERS).get_json()["session_id"]
    other = client.post("/api/start", json={}, headers=HEADERS).get_json()["session_id"]
    upstream.SESSIONS[identifier].recordian_samples = 1_919_999
    url = f"/api/chunk?session_id={identifier}"
    assert (
        client.post(url, data=b"\x00" * 4, content_type="application/octet-stream", headers=HEADERS).status_code == 200
    )
    assert (
        client.post(url, data=b"\x00" * 4, content_type="application/octet-stream", headers=HEADERS).status_code == 400
    )
    assert identifier not in upstream.SESSIONS
    assert other in upstream.SESSIONS
    assert [name for name, _ in calls] == ["start", "start", "chunk"]


@pytest.mark.parametrize("seconds", [0.1, 120, 120.1])
def test_qwen_real_decoder_enforces_upload_duration(qwen_app, seconds):
    app, _upstream, calls, _gc = qwen_app
    response = app.test_client().post(
        "/v1/audio/transcriptions",
        data={"file": (io.BytesIO(_wav_bytes(seconds=seconds)), "audio.wav")},
        headers=HEADERS,
    )
    if seconds <= 120:
        assert response.status_code == 200
        assert [name for name, _ in calls] == ["upload"]
    else:
        assert response.status_code == 400
        assert calls == []


@pytest.mark.parametrize("target", ["asr", "qwen", "bridge"])
@pytest.mark.parametrize("guard", ["origin", "host"])
def test_http_browser_guards_reject_before_audio_or_model(model, qwen_app, monkeypatch, target, guard):
    _instance, asr_calls = model
    qwen, _upstream, qwen_calls, _gc = qwen_app
    if target == "asr":
        # DNS rebinding matters specifically for the backward-compatible no-token mode.
        monkeypatch.setattr(asr_server, "auth_token", "")
        app, path = asr_server.app, "/transcribe"
        kwargs = {"json": _audio_payload()}
    elif target == "qwen":
        app, path = qwen, "/api/start"
        kwargs = {"json": {}}
    else:
        app, path = bridge.app, "/v1/audio/transcriptions"
        kwargs = {"data": {"file": (io.BytesIO(_wav_bytes()), "audio.wav")}}

        def forbidden(*args, **kwargs):
            pytest.fail("browser request reached decoder/upstream")

        monkeypatch.setattr(bridge, "_pcm16_mono", forbidden)
        monkeypatch.setattr(bridge, "_transcribe", forbidden)
    headers = dict(HEADERS)
    headers.update({"Origin": "https://attacker.invalid"} if guard == "origin" else {"Host": "attacker.invalid"})
    if target == "qwen" and guard == "host":
        # This instance uses auth, so rebinding cannot authorize a request: omit token.
        headers.pop("Authorization")
        expected = 401
    else:
        expected = 403
    response = app.test_client().post(path, headers=headers, **kwargs)
    assert response.status_code == expected
    assert asr_calls == []
    assert qwen_calls == []


@pytest.mark.parametrize("base", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_unauthenticated_loopback_same_origin_keeps_legacy_http_working(model, monkeypatch, base):
    _instance, calls = model
    monkeypatch.setattr(asr_server, "auth_token", "")
    response = asr_server.app.test_client().post(
        "/transcribe", base_url=base, json=_audio_payload(), headers={"Origin": base}
    )
    assert response.status_code == 200
    assert calls == [64]


def test_qwen_concurrent_post_returns_429_then_restores_slot(qwen_app, monkeypatch):
    app, upstream, calls, _gc = qwen_app
    entered, release = threading.Event(), threading.Event()
    original = upstream.asr.init_streaming_state

    def slow_start(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)

    monkeypatch.setattr(upstream.asr, "init_streaming_state", slow_start)
    replies = []
    first = threading.Thread(
        target=lambda: replies.append(app.test_client().post("/api/start", json={}, headers=HEADERS))
    )
    first.start()
    try:
        assert entered.wait(3)
        assert app.test_client().post("/api/start", json={}, headers=HEADERS).status_code == 429
        assert calls == []
    finally:
        release.set()
        first.join(timeout=5)
    assert not first.is_alive()
    assert replies[0].status_code == 200
    assert app.test_client().post("/api/start", json={}, headers=HEADERS).status_code == 200
    assert len(calls) == 2


def test_qwen_exception_and_413_release_post_slot(qwen_app, monkeypatch):
    app, upstream, _calls, _gc = qwen_app
    original = upstream.asr.init_streaming_state

    def fail(**kwargs):
        raise RuntimeError("synthetic-start-failure")

    monkeypatch.setattr(upstream.asr, "init_streaming_state", fail)
    client = app.test_client()
    assert client.post("/api/start", json={}, headers=HEADERS).status_code == 500
    monkeypatch.setattr(upstream.asr, "init_streaming_state", original)
    assert client.post("/api/start", json={}, headers=HEADERS).status_code == 200
    response = client.post(
        "/v1/audio/transcriptions",
        data=b"",
        headers=HEADERS,
        content_type="multipart/form-data; boundary=review",
        environ_overrides={"CONTENT_LENGTH": str(16 * 1024 * 1024 + 1)},
    )
    assert response.status_code == 413
    assert client.post("/api/start", json={}, headers=HEADERS).status_code == 200


@pytest.mark.parametrize("qwen_app", [False], indirect=True)
@pytest.mark.parametrize("headers", [{"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}])
def test_unauthenticated_qwen_browser_guards_reject_before_model(qwen_app, headers):
    app, upstream, calls, _gc = qwen_app
    response = app.test_client().post("/api/start", json={}, headers=headers)
    assert response.status_code == 403
    assert calls == []
    assert upstream.SESSIONS == {}


@contextmanager
def _flask_tls_server(app, certificate=None, **options):
    from recordian.http_service import make_http_server

    context = local_auth.server_tls_context(str(certificate[0]), str(certificate[1])) if certificate else None
    server = make_http_server(app, "127.0.0.1", 0, context, **options)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive(), "ephemeral Flask server did not shut down"


def test_real_https_flask_auth_before_fake_model(model, certificates):
    _instance, calls = model
    context = ssl.create_default_context(cafile=str(certificates[0][0]))
    with _flask_tls_server(asr_server.app, certificates[0]) as server:
        for token, expected in [("wrong", 401), (TOKEN, 200)]:
            conn = http.client.HTTPSConnection("127.0.0.1", server.server_port, timeout=2, context=context)
            try:
                conn.request(
                    "POST",
                    "/transcribe",
                    body=json.dumps(_audio_payload()),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                response = conn.getresponse()
                assert response.status == expected
                response.read()
            finally:
                conn.close()
    assert calls == [64]


def test_stalled_http_tls_handshake_does_not_block_other_clients(model, certificates, monkeypatch):
    _instance, _calls = model
    context = ssl.create_default_context(cafile=str(certificates[0][0]))
    with _flask_tls_server(asr_server.app, certificates[0]) as server:
        accepting = threading.Event()
        original = server.get_request

        def observe_accept():
            accepting.set()
            return original()

        monkeypatch.setattr(server, "get_request", observe_accept)
        raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
        conn = http.client.HTTPSConnection("127.0.0.1", server.server_port, timeout=1, context=context)
        try:
            assert accepting.wait(1), "test server never accepted the stalled connection"
            conn.request("GET", "/health", headers=HEADERS)
            response = conn.getresponse()
            assert response.status == 200
            response.read()
        finally:
            # Unblock the accept loop even when the expected regression fails.
            raw.close()
            conn.close()


@pytest.fixture
def transport_app():
    app = Flask("network-review-transport")
    complete_bodies = []

    @app.post("/echo")
    def echo():
        data = request.get_data()
        complete_bodies.append(data)
        return jsonify({"bytes": len(data)})

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    return app, complete_bodies


@contextmanager
def _wire_socket(server, certificate=None):
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as raw:
        if certificate:
            context = ssl.create_default_context(cafile=str(certificate[0]))
            sock = context.wrap_socket(raw, server_hostname="127.0.0.1")
        else:
            sock = raw
        try:
            yield sock
        finally:
            if sock is not raw:
                sock.close()


def _read_to_close(sock):
    result = bytearray()
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            result.extend(data)
    except (ConnectionError, ssl.SSLEOFError):
        pass
    return bytes(result)


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("phase", ["headers", "body", "chunked"])
def test_http_absolute_receive_deadline_releases_eight_dripping_workers(
    transport_app,
    certificates,
    encrypted,
    phase,
):
    app, complete = transport_app
    certificate = certificates[0] if encrypted else None
    sockets = []
    stop = threading.Event()
    with _flask_tls_server(app, certificate, receive_timeout=0.6) as server:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for _ in range(8):
                sock = stack.enter_context(_wire_socket(server, certificate))
                sockets.append(sock)
                if phase == "headers":
                    sock.sendall(b"POST /echo HTTP/1.1\r\nHost: localhost\r\nX-Drip: ")
                elif phase == "body":
                    sock.sendall(b"POST /echo HTTP/1.1\r\nHost: localhost\r\nContent-Length: 999\r\n\r\nx")
                else:
                    sock.sendall(
                        b"POST /echo HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n3e7\r\nx"
                    )

            def drip():
                while not stop.wait(0.04):
                    for sock in sockets:
                        try:
                            sock.sendall(b"x")
                        except OSError:
                            pass

            sender = threading.Thread(target=drip)
            started = time.monotonic()
            sender.start()
            try:
                replies = [_read_to_close(sock) for sock in sockets]
            finally:
                stop.set()
                sender.join(timeout=2)
            assert time.monotonic() - started < 1.5, "drips reset the header/body receive deadline"
            assert complete == []
            if phase != "headers":
                assert all(b"408" in reply for reply in replies)
        # Worker slots must recover, including after read errors inside Flask's body parser.
        with _wire_socket(server, certificate) as sock:
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert b"200 OK" in _read_to_close(sock)


@pytest.mark.parametrize("encrypted", [False, True])
def test_http_worker_limit_rejects_ninth_and_recovers_after_timeouts(
    transport_app,
    certificates,
    monkeypatch,
    encrypted,
):
    app, _complete = transport_app
    certificate = certificates[0] if encrypted else None
    with _flask_tls_server(app, certificate, handshake_timeout=0.6, receive_timeout=0.6) as server:
        entered, all_done = threading.Event(), threading.Event()
        lock = threading.Lock()
        active = peak = 0
        original = server.process_request_thread

        def observe_worker(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 8:
                    entered.set()
            try:
                return original(*args)
            finally:
                with lock:
                    active -= 1
                    if active == 0:
                        all_done.set()

        monkeypatch.setattr(server, "process_request_thread", observe_worker)
        sockets = [socket.create_connection(("127.0.0.1", server.server_port), timeout=2) for _ in range(8)]
        try:
            assert entered.wait(1), "eight workers were never admitted"
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as ninth:
                started = time.monotonic()
                assert ninth.recv(1) == b""
                assert time.monotonic() - started < 0.3, "overload waited for a ninth worker"
            assert all_done.wait(2), "expired connections did not release workers"
            assert peak == 8
            with _wire_socket(server, certificate) as sock:
                sock.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                assert b"200 OK" in _read_to_close(sock)
        finally:
            for sock in sockets:
                sock.close()


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("chunked", [False, True])
def test_http_receive_deadline_does_not_limit_inference_after_body_complete(certificates, encrypted, chunked):
    app = Flask("network-review-slow-inference")
    release = threading.Event()

    @app.post("/infer")
    def infer():
        assert request.get_data() == b"abc"
        assert release.wait(2)
        return jsonify({"text": "synthetic-result"})

    certificate = certificates[0] if encrypted else None
    with _flask_tls_server(app, certificate, receive_timeout=0.15) as server:
        with _wire_socket(server, certificate) as sock:
            if chunked:
                framing, body = b"Transfer-Encoding: chunked\r\n", b"3\r\nabc\r\n0\r\n\r\n"
            else:
                framing, body = b"Content-Length: 3\r\n", b"abc"
            timer = threading.Timer(0.4, release.set)
            timer.start()
            try:
                sock.sendall(b"POST /infer HTTP/1.1\r\nHost: localhost\r\n" + framing + b"\r\n" + body)
                response = _read_to_close(sock)
                assert b"200 OK" in response
                assert b"synthetic-result" in response
            finally:
                release.set()
                timer.join(timeout=2)


@pytest.mark.parametrize("encrypted", [False, True])
def test_http_headers_and_body_share_one_receive_deadline(transport_app, certificates, encrypted):
    app, complete = transport_app
    certificate = certificates[0] if encrypted else None
    stop = threading.Event()
    with _flask_tls_server(app, certificate, receive_timeout=0.6) as server:
        with _wire_socket(server, certificate) as sock:
            started = time.monotonic()
            sock.sendall(b"POST /echo HTTP/1.1\r\nHost: localhost\r\n")
            stop.wait(0.4)
            sock.sendall(b"Content-Length: 999\r\n\r\nx")

            def drip():
                while not stop.wait(0.04):
                    try:
                        sock.sendall(b"x")
                    except OSError:
                        return

            sender = threading.Thread(target=drip)
            sender.start()
            try:
                response = _read_to_close(sock)
                assert b"408" in response
                assert time.monotonic() - started < 0.85, "headers reset the body receive budget"
                assert complete == []
            finally:
                stop.set()
                sender.join(timeout=2)


def test_http_fragmented_tls_handshake_has_absolute_deadline(transport_app, certificates):
    app, complete = transport_app
    context = ssl.create_default_context(cafile=str(certificates[0][0]))
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_side=False, server_hostname="127.0.0.1")
    with pytest.raises(ssl.SSLWantReadError):
        client.do_handshake()
    hello = outgoing.read()
    stop = threading.Event()
    with _flask_tls_server(app, certificates[0], handshake_timeout=0.6) as server:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as raw:
            started = time.monotonic()
            raw.sendall(hello[:10])

            def drip():
                for byte in hello[10:]:
                    if stop.wait(0.04):
                        return
                    try:
                        raw.sendall(bytes([byte]))
                    except OSError:
                        return

            sender = threading.Thread(target=drip)
            sender.start()
            try:
                assert _read_to_close(raw) == b""
                assert time.monotonic() - started < 1.2, "TLS bytes reset the handshake deadline"
                assert complete == []
            finally:
                stop.set()
                sender.join(timeout=2)


def test_bridge_oversize_request_returns_413_without_decoder_or_upstream(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversize request reached decoder or upstream")

    monkeypatch.setattr(bridge, "_pcm16_mono", forbidden)
    monkeypatch.setattr(bridge, "_transcribe", forbidden)
    response = bridge.app.test_client().post(
        "/v1/audio/transcriptions",
        data=b"",
        content_type="multipart/form-data; boundary=review",
        environ_overrides={"CONTENT_LENGTH": str(16 * 1024 * 1024 + 1)},
    )
    assert response.status_code == 413


def test_bridge_bad_audio_returns_400_without_upstream(monkeypatch):
    def bad_audio(*args, **kwargs):
        raise ValueError("synthetic-invalid-audio")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid audio reached upstream")

    monkeypatch.setattr(bridge, "_pcm16_mono", bad_audio)
    monkeypatch.setattr(bridge, "_transcribe", forbidden)
    response = bridge.app.test_client().post(
        "/v1/audio/transcriptions",
        data={"file": (io.BytesIO(b"invalid"), "audio.wav")},
    )
    assert response.status_code == 400


def test_paste_logs_do_not_include_text_or_actual_clipboard(tmp_path, desktop, caplog):
    app = paste_agent.RemotePasteAgent(
        paste_agent.build_parser().parse_args(
            ["--no-notify", "--paste-delay-ms", "0", "--commit-backend", "none"],
        )
    )
    with caplog.at_level(logging.INFO, logger=paste_agent.__name__):
        assert app.handle_payload({"action": "paste", "text": "synthetic-private-text"})["status"] == "ok"
        response = app.handle_payload(
            {
                "action": "paste_only",
                "expected_text": "synthetic-expected-private",
                "preview": "synthetic-private-preview",
                "clipboard_wait_s": 0,
            }
        )
        assert response["detail"] == "clipboard_not_synced"
        assert app.handle_payload({"action": "paste_only", "preview": "synthetic-private-preview"})["status"] == "ok"
    for private in (
        "synthetic-private-text",
        "synthetic-private-clipboard",
        "synthetic-expected-private",
        "synthetic-private-preview",
    ):
        assert private not in caplog.text
