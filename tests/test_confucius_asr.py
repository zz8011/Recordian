"""Tests for the Confucius4-R2T2 streaming ASR provider.

Two layers:
- Frame-level fake-socket unit tests for the exact protocol shape (header,
  PCM frames, incremental merge, reset semantics, CLOSE-code handling,
  error/timeout/cancel paths, backpressure).
- Real loopback WebSocket tests: a ``websockets`` server implementing the
  pinned protocol against the real ``websocket-client`` transport — covering
  the finish race (ordinary resets/deltas during finish), abnormal close
  codes, missing final, server errors, malformed payloads and >64-frame
  file feeding with a slow consumer.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from recordian.providers.confucius_asr import (
    _SEND_QUEUE_MAX_FRAMES,
    EOS_MESSAGE,
    MAX_SYSTEM_PROMPT_CHARS,
    REALTIME_CHUNK_SIZE_SEC,
    ConfuciusASRProvider,
    ConfuciusProtocolError,
    ConfuciusRealtimeSession,
    compose_system_prompt,
    f32le_to_pcm16le,
    resolve_ws_endpoint,
    sanitize_ws_url,
    wav_file_to_pcm16le,
)

_OP_TEXT = 0x1
_OP_CLOSE = 0x8


# ---------------------------------------------------------------------------
# Endpoint / prompt / PCM helpers
# ---------------------------------------------------------------------------
class TestResolveWsEndpoint:
    def test_ws_url_passthrough(self):
        assert resolve_ws_endpoint("ws://127.0.0.1:8272/asr_stream_api_v1") == "ws://127.0.0.1:8272/asr_stream_api_v1"

    def test_http_translated_to_ws(self):
        assert resolve_ws_endpoint("http://192.168.5.111:8272/asr_stream_api_v1") == "ws://192.168.5.111:8272/asr_stream_api_v1"

    def test_https_translated_to_wss(self):
        assert resolve_ws_endpoint("https://example.com/asr") == "wss://example.com/asr"

    def test_bare_host_gets_default_path(self):
        assert resolve_ws_endpoint("127.0.0.1:8272") == "ws://127.0.0.1:8272/asr_stream_api_v1"

    def test_empty_path_gets_default(self):
        assert resolve_ws_endpoint("ws://127.0.0.1:8272/") == "ws://127.0.0.1:8272/asr_stream_api_v1"

    def test_userinfo_and_query_stripped(self):
        # Credentials must never ride in the URL; query/fragment dropped too.
        assert (
            resolve_ws_endpoint("ws://user:p%40ss@127.0.0.1:8272/asr?token=abc#frag")
            == "ws://127.0.0.1:8272/asr"
        )

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="asr_realtime_endpoint"):
            resolve_ws_endpoint("  ")

    def test_bad_scheme_rejected(self):
        with pytest.raises(ValueError, match="ws"):
            resolve_ws_endpoint("ftp://127.0.0.1:21/x")


class TestComposeSystemPrompt:
    def test_hotwords_and_context_combined(self):
        prompt = compose_system_prompt("会议记录", ["露西", "小二"])
        assert "会议记录" in prompt
        assert "露西" in prompt and "小二" in prompt

    def test_hotwords_survive_tight_budget(self):
        base = "长上下文" * 100
        hotwords = [f"热词{i}" for i in range(30)]
        prompt = compose_system_prompt(base, hotwords, max_chars=200)
        assert len(prompt) <= 200
        # hotwords are preserved; the free-form base context is shortened first
        for word in hotwords:
            assert word in prompt

    def test_budget_caps_total_length(self):
        prompt = compose_system_prompt("", ["甲", "乙"], max_chars=MAX_SYSTEM_PROMPT_CHARS)
        assert len(prompt) <= MAX_SYSTEM_PROMPT_CHARS

    def test_hotwords_alone_trimmed_from_tail_when_over_budget(self):
        hotwords = [f"很长很长的热词{i:03d}" for i in range(40)]
        prompt = compose_system_prompt("", hotwords, max_chars=120)
        assert len(prompt) <= 120
        assert "热词000" in prompt  # leading terms kept

    def test_dedupe_and_cap(self):
        prompt = compose_system_prompt("", ["甲", "甲", "乙"], max_hotwords=40)
        assert prompt.count("甲") == 1


class TestPcmConversion:
    """f32le -> PCM16 is standard-library only; numpy appears only in tests."""

    def test_roundtrip_values(self):
        samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype="<f4")
        pcm = f32le_to_pcm16le(samples.tobytes())
        values = struct.unpack("<5h", pcm)
        assert values[0] == 0
        assert values[1] == 16384  # round(0.5 * 32767)
        assert values[2] == -16384
        assert values[3] == 32767
        assert values[4] == -32767

    def test_clipping_and_non_finite(self):
        samples = np.array([2.0, -3.0, np.nan, np.inf, -np.inf], dtype="<f4")
        pcm = f32le_to_pcm16le(samples.tobytes())
        values = struct.unpack("<5h", pcm)
        assert values == (32767, -32767, 0, 32767, -32767)

    def test_unaligned_frame_rejected(self):
        with pytest.raises(ValueError, match="multiple of 4"):
            f32le_to_pcm16le(b"\x00\x00\x00")

    def test_empty_frame(self):
        assert f32le_to_pcm16le(b"") == b""


class TestWavInput:
    def _write_wav(self, path: Path, *, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
        frames = struct.pack("<8h", *range(8))
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            handle.writeframes(frames * (channels if channels > 1 else 1))
        return frames

    def test_16k_mono_s16_passthrough(self, tmp_path):
        path = tmp_path / "ok.wav"
        frames = self._write_wav(path)
        assert wav_file_to_pcm16le(path) == frames

    def test_mismatched_wav_converted_explicitly(self, tmp_path, monkeypatch):
        path = tmp_path / "hi.wav"
        self._write_wav(path, rate=44100)
        monkeypatch.setattr("recordian.providers.confucius_asr.which", lambda name: "/usr/bin/ffmpeg")

        class _Proc:
            returncode = 0
            stdout = b"\x01\x00" * 16
            stderr = b""

        calls = []

        def _run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return _Proc()

        monkeypatch.setattr("recordian.providers.confucius_asr.subprocess.run", _run)
        out = wav_file_to_pcm16le(path)
        assert out == b"\x01\x00" * 16
        cmd, kwargs = calls[0]
        assert "-ar" in cmd and "16000" in cmd
        assert kwargs.get("timeout"), "ffmpeg conversion must carry an explicit timeout"

    def test_ffmpeg_timeout_is_explainable(self, tmp_path, monkeypatch):
        path = tmp_path / "slow.wav"
        self._write_wav(path, rate=44100)
        monkeypatch.setattr("recordian.providers.confucius_asr.which", lambda name: "/usr/bin/ffmpeg")

        def _run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        monkeypatch.setattr("recordian.providers.confucius_asr.subprocess.run", _run)
        with pytest.raises(RuntimeError, match="timed out"):
            wav_file_to_pcm16le(path)

    def test_ffmpeg_failure_reports_exit_code(self, tmp_path, monkeypatch):
        path = tmp_path / "bad.wav"
        self._write_wav(path, rate=44100)
        monkeypatch.setattr("recordian.providers.confucius_asr.which", lambda name: "/usr/bin/ffmpeg")

        class _Proc:
            returncode = 3
            stdout = b""
            stderr = b"bogus codec"

        monkeypatch.setattr(
            "recordian.providers.confucius_asr.subprocess.run", lambda cmd, **kwargs: _Proc()
        )
        with pytest.raises(RuntimeError, match="exit code 3"):
            wav_file_to_pcm16le(path)

    def test_ogg_never_read_as_pcm(self, tmp_path, monkeypatch):
        path = tmp_path / "audio.ogg"
        path.write_bytes(b"OggS fake payload")
        monkeypatch.setattr("recordian.providers.confucius_asr.which", lambda name: None)
        with pytest.raises(RuntimeError, match="ffmpeg"):
            wav_file_to_pcm16le(path)


# ---------------------------------------------------------------------------
# Fake websocket for protocol unit tests (frame-level, like websocket-client)
# ---------------------------------------------------------------------------
class _FakeClosed(Exception):
    """Simulates a TCP drop without a CLOSE frame (abnormal 1006)."""


class _FakeTimeout(Exception):
    """Simulates a socket read timeout window."""


class _FakeFrame:
    def __init__(self, opcode: int, data: bytes = b""):
        self.opcode = opcode
        self.data = data


class FakeWebSocket:
    """Scriptable stand-in for websocket-client's WebSocket (frame level)."""

    def __init__(self, greeting: object = None, on_eos=None):
        self.sent_text: list[str] = []
        self.sent_binary: list[bytes] = []
        self.closed = False
        self._inbox: queue.Queue = queue.Queue()
        self._on_eos = on_eos
        self.block_binary = threading.Event()
        self.block_binary.set()
        if greeting is not None:
            self.feed(greeting)

    # client-side API -------------------------------------------------------
    def send(self, payload):
        self.sent_text.append(payload)
        if payload == EOS_MESSAGE and self._on_eos is not None:
            self._on_eos(self)

    def send_binary(self, payload):
        self.block_binary.wait(timeout=5)
        self.sent_binary.append(bytes(payload))

    def recv_data_frame(self, control_frame: bool = False):
        item = self._inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    def pong(self, data: bytes = b""):
        pass

    def close(self, timeout=None):
        self.closed = True
        self._inbox.put(_FakeClosed())

    # server-side scripting -------------------------------------------------
    def feed(self, message):
        if isinstance(message, (dict, list)):
            message = json.dumps(message, ensure_ascii=False)
        frame = _FakeFrame(_OP_TEXT, message.encode("utf-8"))
        self._inbox.put((_OP_TEXT, frame))

    def feed_raw(self, raw: str):
        frame = _FakeFrame(_OP_TEXT, raw.encode("utf-8"))
        self._inbox.put((_OP_TEXT, frame))

    def feed_close(self, code: int | None = 1000):
        data = struct.pack("!H", code) if code is not None else b""
        self._inbox.put((_OP_CLOSE, _FakeFrame(_OP_CLOSE, data)))

    def feed_drop(self):
        self._inbox.put(_FakeClosed())


class _FakeWSModuleExceptions:
    WebSocketTimeoutException = _FakeTimeout


class FakeWSModule(_FakeWSModuleExceptions):
    def __init__(self, socket: FakeWebSocket):
        self.socket = socket
        self.urls: list[str] = []

    def create_connection(self, url, timeout=None):
        self.urls.append(url)
        return self.socket


def _connected_greeting(request_id: str = "") -> dict:
    return {"status": "connected", "requestId": request_id, "msg": "", "active_connections": 1}


def _success(text: str, *, reset: bool = False) -> dict:
    return {"status": "success", "requestId": "r", "msg": {"text": text, "reset": reset}}


def _make_session(socket: FakeWebSocket, **overrides) -> ConfuciusRealtimeSession:
    kwargs = {
        "ws_url": "ws://127.0.0.1:8272/asr_stream_api_v1",
        "api_key": "sekrit",
        "language": "Chinese",
        "system_prompt": "热词参考: 露西",
        "timeout_s": 2.0,
        "use_vad": False,
        "ws_module": FakeWSModule(socket),
    }
    kwargs.update(overrides)
    session = ConfuciusRealtimeSession(**kwargs)
    session.start()
    return session


def _f32_frame(n_samples: int = 160) -> bytes:
    return np.zeros(n_samples, dtype="<f4").tobytes()


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------
class TestHandshake:
    def test_header_shape(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        try:
            header = json.loads(ws.sent_text[0])
            assert header["requestId"]
            assert header["channels"] == 1
            assert header["sample_rate"] == 16000
            assert header["language"] == "Chinese"
            assert header["secret_key"] == "sekrit"
            assert header["system_prompt"] == "热词参考: 露西"
            assert header["use_vad"] is False
            assert header["mode"] == "slow"
        finally:
            session.cancel()

    def test_language_auto_maps_to_zhen(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws, language="")
        try:
            header = json.loads(ws.sent_text[0])
            assert header["language"] == "zhen"
        finally:
            session.cancel()

    def test_no_api_key_omits_secret(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws, api_key=None)
        try:
            header = json.loads(ws.sent_text[0])
            assert "secret_key" not in header
        finally:
            session.cancel()

    def test_error_greeting_fails(self):
        ws = FakeWebSocket(greeting={"status": "error", "msg": "json header is expected"})
        with pytest.raises(ConfuciusProtocolError, match="connected"):
            _make_session(ws)
        assert ws.closed

    def test_close_frame_during_handshake_reports_code(self):
        ws = FakeWebSocket()
        ws.feed_close(1000)
        with pytest.raises(ConfuciusProtocolError, match="code 1000"):
            _make_session(ws)

    def test_auth_close_4401_is_explained(self):
        # Real server: wrong secret_key -> CLOSE 4401, no error JSON.
        ws = FakeWebSocket()
        ws.feed_close(4401)
        with pytest.raises(ConfuciusProtocolError, match="4401|unauthorized"):
            _make_session(ws)

    def test_drop_during_handshake_fails(self):
        ws = FakeWebSocket()
        ws.feed_drop()
        with pytest.raises(ConfuciusProtocolError, match="handshake"):
            _make_session(ws)

    def test_non_json_greeting_fails(self):
        ws = FakeWebSocket(greeting="not-json{{{")
        with pytest.raises(ConfuciusProtocolError, match="JSON"):
            _make_session(ws)

    def test_api_key_never_logged(self, caplog):
        ws = FakeWebSocket()
        ws.feed_drop()
        with caplog.at_level("DEBUG"):
            with pytest.raises(ConfuciusProtocolError):
                _make_session(ws, api_key="top-secret-value")
        assert "top-secret-value" not in caplog.text

    def test_connect_error_never_echoes_url_userinfo_or_key(self):
        class _BoomModule:
            WebSocketTimeoutException = _FakeTimeout

            def create_connection(self, url, timeout=None):
                raise OSError(f"dial {url} refused; Authorization: sekrit")

        session = ConfuciusRealtimeSession(
            ws_url="ws://user:p%40ss@127.0.0.1:8272/asr_stream_api_v1",
            api_key="sekrit",
            language="",
            system_prompt="",
            timeout_s=2.0,
            use_vad=False,
            ws_module=_BoomModule(),
        )
        with pytest.raises(ConfuciusProtocolError) as excinfo:
            session.start()
        msg = str(excinfo.value)
        assert "sekrit" not in msg
        assert "user" not in msg
        assert "p%40ss" not in msg
        assert "OSError" in msg  # only the sanitized type name survives

    def test_safe_url_strips_userinfo_and_query(self):
        assert (
            sanitize_ws_url("ws://user:pass@127.0.0.1:8272/asr?token=abc")
            == "ws://127.0.0.1:8272/asr"
        )


# ---------------------------------------------------------------------------
# Incremental merge / reset / snapshot
# ---------------------------------------------------------------------------
class TestStreaming:
    def test_pcm_frames_are_binary_pcm16(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        try:
            session.push_audio(_f32_frame(160))
            deadline = time.monotonic() + 2
            while not ws.sent_binary and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ws.sent_binary, "sender thread never forwarded the frame"
            frame = ws.sent_binary[0]
            assert len(frame) == 160 * 2  # 160 float32 samples -> 160 int16
        finally:
            session.cancel()

    def test_incremental_merge_and_reset_keeps_accumulation(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        try:
            ws.feed(_success("你好"))
            ws.feed({})  # keepalive frame: ignored
            ws.feed(_success("，世界", reset=True))  # segment boundary, not a wipe
            ws.feed(_success("！"))
            ws.feed(_success(""))  # empty text: no-op
            deadline = time.monotonic() + 2
            snapshot = ""
            while time.monotonic() < deadline:
                snapshot = session.push_audio(b"")["text"]
                if snapshot == "你好，世界！":
                    break
                time.sleep(0.01)
            assert snapshot == "你好，世界！"
        finally:
            session.cancel()

    def test_push_audio_is_snapshot_not_inference_wait(self):
        # The fake never sends any partial: push_audio must still return fast.
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        try:
            start = time.perf_counter()
            result = session.push_audio(_f32_frame())
            assert time.perf_counter() - start < 0.5
            assert result == {"text": ""}
        finally:
            session.cancel()

    def test_send_queue_overflow_fails_explicitly(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        ws.block_binary.clear()  # sender stuck: queue fills up
        session = _make_session(ws)
        try:
            with pytest.raises(RuntimeError, match="overflow"):
                for _ in range(200):
                    session.push_audio(_f32_frame(16))
        finally:
            ws.block_binary.set()
            session.cancel()

    def test_realtime_backlog_capped_near_one_second(self):
        # 6 frames * 160 ms = 0.96 s — the realtime latency ceiling.
        assert _SEND_QUEUE_MAX_FRAMES * REALTIME_CHUNK_SIZE_SEC <= 1.05

    def test_read_timeout_is_not_an_error_and_not_a_busy_loop(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        try:
            # A socket read timeout window must be absorbed, not kill the stream.
            ws._inbox.put(_FakeTimeout())
            ws.feed(_success("仍在"))
            deadline = time.monotonic() + 2
            snapshot = ""
            while time.monotonic() < deadline:
                snapshot = session.push_audio(b"")["text"]
                if snapshot == "仍在":
                    break
                time.sleep(0.01)
            assert snapshot == "仍在"
        finally:
            session.cancel()


# ---------------------------------------------------------------------------
# finish / cancel / failure modes
# ---------------------------------------------------------------------------
class TestFinishCancel:
    def _finish_ok(self, ws: FakeWebSocket):
        ws.feed(_success(" final", reset=True))
        ws.feed_close(1000)

    def test_finish_returns_full_transcript(self):
        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=self._finish_ok)
        session = _make_session(ws)
        ws.feed(_success("你好"))
        result = session.finish()
        assert result.text == "你好 final"
        assert EOS_MESSAGE in ws.sent_text
        assert result.metadata["realtime"] is True
        # idempotent: second finish returns the same result, no second EOS
        eos_count = ws.sent_text.count(EOS_MESSAGE)
        again = session.finish()
        assert again.text == "你好 final"
        assert ws.sent_text.count(EOS_MESSAGE) == eos_count

    def test_ordinary_reset_during_finish_does_not_truncate(self):
        """The core race: after EOS the server drains queued audio, sending
        ordinary deltas and an ordinary segment reset BEFORE the final reset.
        The first post-EOS reset must not end accumulation."""

        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("排队音频"))  # still draining pre-EOS audio
            ws.feed(_success("普通分段", reset=True))  # ordinary reset, not final
            ws.feed(_success("剩余文字"))
            ws.feed(_success("。", reset=True))  # final reset
            ws.feed_close(1000)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        ws.feed(_success("前文"))
        result = session.finish()
        assert result.text == "前文排队音频普通分段剩余文字。"
        assert result.metadata["segments"] == 2

    def test_ordinary_reset_then_keepalive_then_close_fails(self):
        """Grok P1 case 1: an ordinary post-EOS reset followed by a keepalive
        is NOT the final — upstream sends the final reset and closes with no
        keepalive in between. Must fail, not return the partial transcript."""

        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("普通分段", reset=True))
            ws.feed({})  # keepalive after the reset: invalidates finalness
            ws.feed_close(1000)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        ws.feed(_success("前文"))
        with pytest.raises(ConfuciusProtocolError, match="missing EOF"):
            session.finish()

    def test_ordinary_reset_then_delta_then_close_fails(self):
        """Grok P1 case 2: the last data frame before close is a non-reset
        delta. The EOS final is always a trailing reset — must fail."""

        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("普通分段", reset=True))
            ws.feed(_success("还没结束"))  # trailing delta: invalidates
            ws.feed_close(1000)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        with pytest.raises(ConfuciusProtocolError, match="missing EOF"):
            session.finish()

    def test_reset_then_delta_then_real_final_still_succeeds(self):
        """Invalidation must not break the healthy drain: ordinary reset ->
        further delta -> true final reset -> clean close stays a success."""

        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("排队", reset=False))
            ws.feed(_success("普通分段", reset=True))
            ws.feed(_success("剩余", reset=False))
            ws.feed(_success("。", reset=True))
            ws.feed_close(1000)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        ws.feed(_success("前文"))
        result = session.finish()
        assert result.text == "前文排队普通分段剩余。"

    def test_final_reset_processed_before_eos_send_returns(self):
        """Deterministic EOS-arm window: the fake holds ws.send(EOS) open
        until the receiver thread has already processed the final reset and
        the CLOSE frame. If arming happened only after send() returned, this
        would deterministically fail with a false 'missing EOF'."""
        holder: dict[str, ConfuciusRealtimeSession] = {}

        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("即时最终", reset=True))
            ws.feed_close(1000)
            # Block the finish thread inside send() until the receiver has
            # drained BOTH frames — the window is exercised deterministically.
            session = holder["session"]
            assert session._done.wait(timeout=2), "receiver never drained the final frames"

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        holder["session"] = session
        result = session.finish()
        assert result.text == "即时最终"

    def test_eos_send_error_always_fails(self):
        def _on_eos(ws: FakeWebSocket):
            raise OSError("socket gone")  # send(EOS) itself blows up

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        with pytest.raises(ConfuciusProtocolError, match="failed to send EOS"):
            session.finish()
        # The send error is sticky state, not just the raised exception.
        with session._lock:
            assert session._error is not None
            assert "failed to send EOS" in session._error

    def test_final_reset_without_clean_close_fails(self):
        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("最终", reset=True))
            ws.feed_close(1011)  # server error close: NOT a success

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        with pytest.raises(ConfuciusProtocolError, match="1011"):
            session.finish()

    def test_close_without_status_code_fails(self):
        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("最终", reset=True))
            ws.feed_close(None)  # no status code (1005)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        with pytest.raises(ConfuciusProtocolError, match="1005"):
            session.finish()

    def test_abnormal_drop_after_final_reset_fails(self):
        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("最终", reset=True))
            ws.feed_drop()  # TCP dies without CLOSE frame (1006)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        with pytest.raises(RuntimeError, match="dropped|failed"):
            session.finish()

    def test_abnormal_close_before_finish_fails(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        ws.feed_drop()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                session.push_audio(b"")
            except RuntimeError:
                break
            time.sleep(0.01)
        with pytest.raises(RuntimeError, match="dropped|failed"):
            session.finish()
        session.cancel()  # still safe after failure

    def test_missing_eof_fails(self):
        # Server closes (clean 1000) right after EOS without the final message.
        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=lambda s: s.feed_close(1000))
        session = _make_session(ws)
        with pytest.raises(ConfuciusProtocolError, match="missing EOF"):
            session.finish()

    def test_finish_timeout_fails(self):
        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=lambda s: None)
        session = _make_session(ws, timeout_s=1.0)
        with pytest.raises(TimeoutError, match="did not finish"):
            session.finish()

    def test_finish_uses_one_absolute_deadline(self):
        # Small configured timeout must bound the WHOLE finish, not stack
        # several independent per-step waits.
        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=lambda s: None)
        session = _make_session(ws, timeout_s=1.0)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            session.finish()
        assert time.monotonic() - start < 1.0 + 1.5  # deadline + close budget only

    def test_server_error_status_fails(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        ws.feed({"status": "error", "requestId": "r", "msg": "invalid system_prompt"})
        with pytest.raises(RuntimeError, match="error status"):
            session.finish()

    def test_server_error_message_never_echoed(self):
        # Server-controlled detail may carry transcript/credential material:
        # public errors keep only the safe category, never the raw content.
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        secret_marker = "用户转写内容-secret-9f3a" + "x" * 500
        ws.feed({"status": "error", "msg": secret_marker})
        with pytest.raises(RuntimeError) as excinfo:
            session.finish()
        message = str(excinfo.value)
        assert "error status" in message
        assert secret_marker not in message
        assert "用户转写内容" not in message
        assert "x" * 200 not in message

    def test_malformed_payload_fails(self):
        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("ok", reset=True))
            ws.feed_close(1000)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        session = _make_session(ws)
        ws.feed_raw("not-json{{{")
        with pytest.raises(RuntimeError, match="malformed"):
            session.finish()

    def test_non_object_payload_fails(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        ws.feed_raw("[1,2,3]")
        with pytest.raises(RuntimeError, match="malformed"):
            session.finish()

    def test_cancel_is_idempotent_and_discards_late_packets(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        ws.feed(_success("你好"))
        deadline = time.monotonic() + 2
        while session.push_audio(b"")["text"] != "你好" and time.monotonic() < deadline:
            time.sleep(0.01)
        session.cancel()
        session.cancel()  # idempotent
        ws.feed(_success("迟到包"))
        time.sleep(0.1)
        # after cancel the snapshot stays frozen and finish/cancel don't raise
        with pytest.raises(RuntimeError, match="cancel"):
            session.finish()

    def test_cancel_is_bounded_and_leaks_no_threads(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        session = _make_session(ws)
        start = time.monotonic()
        session.cancel()
        elapsed = time.monotonic() - start
        assert elapsed < 1.5, f"cancel took {elapsed:.2f}s — stacked waits?"
        for thread in (session._sender_thread, session._receiver_thread):
            assert thread is not None and not thread.is_alive(), f"{thread.name} leaked"

    def test_push_after_finish_fails(self):
        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=self._finish_ok)
        session = _make_session(ws)
        session.finish()
        with pytest.raises(RuntimeError, match="already finished"):
            session.push_audio(_f32_frame())


# ---------------------------------------------------------------------------
# Bounded backpressure (file feeding)
# ---------------------------------------------------------------------------
class TestBackpressure:
    def test_blocking_put_waits_and_times_out(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        ws.block_binary.clear()  # sender stuck: queue cannot drain
        session = _make_session(ws)
        try:
            frame = b"\x00\x00" * 2560
            # The sender holds one frame while blocked on the socket, so the
            # queue is full only after capacity + 1 puts.
            for _ in range(_SEND_QUEUE_MAX_FRAMES + 1):
                session.push_pcm16(frame, block=True, timeout_s=0.5)
            # Queue full: a blocking put with a short budget times out —
            # it must NOT raise the realtime overflow error.
            with pytest.raises(TimeoutError, match="did not drain"):
                session.push_pcm16(frame, block=True, timeout_s=0.3)
            # Unblock the socket: the producer can continue within budget.
            ws.block_binary.set()
            session.push_pcm16(frame, block=True, timeout_s=2.0)
        finally:
            ws.block_binary.set()
            session.cancel()

    def test_nonblocking_put_still_overflows_fast(self):
        ws = FakeWebSocket(greeting=_connected_greeting())
        ws.block_binary.clear()
        session = _make_session(ws)
        try:
            frame = b"\x00\x00" * 2560
            with pytest.raises(RuntimeError, match="overflow"):
                for _ in range(_SEND_QUEUE_MAX_FRAMES + 2):
                    session.push_pcm16(frame, block=False)
        finally:
            ws.block_binary.set()
            session.cancel()


# ---------------------------------------------------------------------------
# Provider-level behavior
# ---------------------------------------------------------------------------
class TestProvider:
    def test_capabilities(self):
        provider = ConfuciusASRProvider("ws://127.0.0.1:8272")
        caps = provider.capabilities
        assert caps.supports_realtime is True
        assert caps.supports_hotwords is True
        assert caps.supports_context is True
        assert caps.supports_language_hint is True
        assert provider.provider_name == "confucius-asr"

    def test_declares_official_160ms_chunk(self):
        provider = ConfuciusASRProvider("ws://127.0.0.1:8272")
        assert provider.realtime_chunk_size_sec == pytest.approx(0.16)

    def test_endpoint_required(self):
        with pytest.raises(ValueError):
            ConfuciusASRProvider("")

    def test_transcribe_file_feeds_same_protocol(self, tmp_path):
        frames = struct.pack("<160h", *([0] * 160))
        wav_path = tmp_path / "a.wav"
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(frames)

        def _on_eos(ws: FakeWebSocket):
            ws.feed(_success("整句", reset=True))
            ws.feed_close(1000)

        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=_on_eos)
        provider = ConfuciusASRProvider(
            "ws://127.0.0.1:8272", api_key="k", ws_module=FakeWSModule(ws)
        )
        result = provider.transcribe_file(wav_path, hotwords=["甲"])
        assert result.text == "整句"
        assert ws.sent_binary  # audio went over the wire as binary PCM16

    def test_create_provider_factory(self):
        import argparse

        from recordian.linux_dictate import create_provider

        args = argparse.Namespace(
            asr_provider="confucius-asr",
            asr_realtime_endpoint="ws://127.0.0.1:8272/asr_stream_api_v1",
            asr_api_key="k",
            asr_timeout_s=15.0,
            qwen_language="Chinese",
            asr_context="会议",
            asr_context_preset="",
        )
        provider = create_provider(args)
        assert isinstance(provider, ConfuciusASRProvider)
        assert provider.endpoint == "ws://127.0.0.1:8272/asr_stream_api_v1"
        assert provider.api_key == "k"
        assert provider.timeout_s == 15.0

    def test_create_provider_factory_requires_endpoint(self):
        import argparse

        from recordian.linux_dictate import create_provider

        args = argparse.Namespace(
            asr_provider="confucius-asr",
            asr_realtime_endpoint="",
            asr_api_key="",
            asr_timeout_s=15.0,
            qwen_language="",
            asr_context="",
            asr_context_preset="",
        )
        with pytest.raises(ValueError, match="asr_realtime_endpoint"):
            create_provider(args)


class TestCliContract:
    def test_cli_exposes_confucius_and_semif_args(self):
        from recordian.arg_parser import build_parser

        args = build_parser().parse_args(
            [
                "--asr-provider",
                "confucius-asr",
                "--asr-realtime-endpoint",
                "ws://127.0.0.1:8272/asr_stream_api_v1",
                "--enable-semif-correction",
                "--semif-endpoint",
                "http://192.168.5.111:42032/v1/systemone",
                "--semif-timeout-s",
                "0.2",
            ]
        )
        assert args.asr_provider == "confucius-asr"
        assert args.asr_realtime_endpoint == "ws://127.0.0.1:8272/asr_stream_api_v1"
        assert args.enable_semif_correction is True
        assert args.semif_endpoint == "http://192.168.5.111:42032/v1/systemone"
        assert args.semif_timeout_s == 0.2

    def test_cli_semif_defaults(self):
        from recordian.arg_parser import build_parser

        args = build_parser().parse_args([])
        assert args.enable_semif_correction is False
        assert args.semif_endpoint == ""
        assert args.semif_timeout_s == 0.12


# ---------------------------------------------------------------------------
# Optional-dependency isolation: basic import must not require numpy/ws
# ---------------------------------------------------------------------------
class TestOptionalDependencyIsolation:
    def test_import_and_convert_without_numpy_or_websocket(self):
        code = r'''
import struct
import sys


class _Blocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"numpy", "websocket"}:
            raise ImportError(f"blocked optional dependency: {fullname}")
        return None


sys.meta_path.insert(0, _Blocker())
for mod in ("numpy", "websocket"):
    sys.modules.pop(mod, None)

import recordian.providers  # base registry must import cleanly
from recordian.providers import ConfuciusASRProvider  # lazy export works
from recordian.providers.confucius_asr import f32le_to_pcm16le

pcm = f32le_to_pcm16le(struct.pack("<2f", 0.5, -0.5))
assert struct.unpack("<2h", pcm) == (16384, -16384), pcm

provider = ConfuciusASRProvider("ws://127.0.0.1:8272")
try:
    provider.start_realtime_session(hotwords=[])
except ImportError as exc:
    assert "websocket-client" in str(exc)
else:
    raise SystemExit("expected ImportError for missing websocket-client")
print("ISOLATION-OK")
'''
        import recordian

        src_parent = str(Path(recordian.__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = src_parent + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert "ISOLATION-OK" in proc.stdout


# ---------------------------------------------------------------------------
# Real loopback WebSocket tests (websockets server <-> websocket-client)
# ---------------------------------------------------------------------------
try:
    import websockets.asyncio.server as _websockets_asyncio_server  # noqa: F401

    _HAS_WEBSOCKETS = True
except ImportError:  # loopback tests skip; fake-socket tests still run
    _HAS_WEBSOCKETS = False


class _LoopbackServer:
    """Minimal but faithful implementation of the pinned server protocol.

    The per-test ``scenario(server, ws)`` coroutine runs after the handshake
    and drives the rest of the session (deltas, resets, close behavior).
    """

    def __init__(self, scenario):
        self._scenario = scenario
        self.header: dict | None = None
        self.binary_frames = 0
        self.port: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        assert self._ready.wait(timeout=10), "loopback server did not start"
        return self

    def _run(self):
        import asyncio

        asyncio.run(self._serve())

    async def _serve(self):
        import asyncio

        from websockets.asyncio.server import serve

        async with serve(self._handle, "127.0.0.1", 0) as server:
            sock = server.sockets[0]
            self.port = sock.getsockname()[1]
            self._ready.set()
            await asyncio.Future()  # run until the process ends

    async def _handle(self, ws):
        raw_header = await ws.recv()
        self.header = json.loads(raw_header)
        assert self.header.get("requestId"), "validate_header: requestId required"
        await ws.send(json.dumps({
            "status": "connected",
            "requestId": self.header["requestId"],
            "msg": "",
            "active_connections": 1,
        }))
        await self._scenario(self, ws)

    async def send_success(self, ws, text: str, *, reset: bool = False):
        await ws.send(json.dumps({
            "status": "success",
            "requestId": self.header["requestId"],
            "msg": {"text": text, "reset": reset},
        }, ensure_ascii=False))


def _provider_for(server: _LoopbackServer, **kwargs) -> ConfuciusASRProvider:
    return ConfuciusASRProvider(
        f"ws://127.0.0.1:{server.port}/asr_stream_api_v1",
        api_key="loopback-key",
        timeout_s=5.0,
        language="Chinese",
        **kwargs,
    )


@pytest.mark.skipif(not _HAS_WEBSOCKETS, reason="websockets not installed (loopback test skipped)")
class TestLoopback:
    def test_full_streaming_roundtrip(self):
        async def _scenario(server: _LoopbackServer, ws):
            pieces = ["你好", "世界"]
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "。", reset=True)
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1
                if pieces:
                    piece = pieces.pop(0)
                    await server.send_success(ws, piece, reset=not pieces)

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=["测试热词"])
        try:
            frame = (np.ones(160, dtype="<f4") * 0.01).tobytes()
            snapshots = set()
            for _ in range(6):
                snapshots.add(session.push_audio(frame)["text"])
                time.sleep(0.05)
            assert any("你好" in snap for snap in snapshots)
            result = session.finish()
        finally:
            session.cancel()
        assert result.text == "你好世界。"
        assert server.binary_frames > 0
        # header carried the hotword context; auth value reused as secret_key
        assert "测试热词" in server.header.get("system_prompt", "")
        assert server.header.get("secret_key") == "loopback-key"

    def test_finish_race_accumulates_until_clean_close(self):
        """Real transport proof: ordinary reset + deltas arrive DURING finish
        (server draining queued audio after EOS) before the final reset and a
        clean 1000 close. The full transcript must not be truncated."""

        async def _scenario(server: _LoopbackServer, ws):
            sent_opening = False
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        # Drain of audio queued ahead of EOS: ordinary delta,
                        # an ordinary segment reset, more text, THEN the final.
                        await server.send_success(ws, "排队音频")
                        await server.send_success(ws, "普通分段", reset=True)
                        await server.send_success(ws, "剩余文字")
                        await server.send_success(ws, "。", reset=True)
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1
                if not sent_opening:
                    sent_opening = True
                    await server.send_success(ws, "前文")

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(256, dtype="<f4") * 0.01).tobytes())
        result = session.finish()
        assert result.text == "前文排队音频普通分段剩余文字。"

    def test_close_1011_after_final_reset_fails(self):
        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "最终", reset=True)
                        await ws.close(code=1011)  # server error close
                        return
                    continue
                server.binary_frames += 1

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(ConfuciusProtocolError, match="1011"):
            session.finish()

    def test_abnormal_drop_after_final_reset_fails(self):
        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "最终", reset=True)
                        ws.transport.abort()  # TCP dies: no CLOSE frame (1006)
                        return
                    continue
                server.binary_frames += 1

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(RuntimeError, match="dropped|failed"):
            session.finish()

    def test_missing_final_message_fails(self):
        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await ws.close(code=1000)  # clean close, but NO final
                        return
                    continue
                server.binary_frames += 1

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(ConfuciusProtocolError, match="missing EOF"):
            session.finish()

    def test_sender_error_fails(self):
        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "最终", reset=True)
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1
                await ws.send(json.dumps({
                    "status": "error",
                    "requestId": server.header["requestId"],
                    "msg": "boom from server with transcript 用户内容",
                }))

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(RuntimeError) as excinfo:
            session.finish()
        # The safe category survives; the server-controlled detail does not.
        assert "error status" in str(excinfo.value)
        assert "boom from server" not in str(excinfo.value)
        assert "用户内容" not in str(excinfo.value)

    def test_ordinary_reset_keepalive_then_close_1000_fails(self):
        """Real transport, Grok P1 case 1: ordinary reset -> keepalive ->
        clean close. No EOS final ever arrived — must fail."""

        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "普通分段", reset=True)
                        await ws.send("{}")
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1
                if server.binary_frames == 1:
                    await server.send_success(ws, "前文")

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(ConfuciusProtocolError, match="missing EOF"):
            session.finish()

    def test_ordinary_reset_delta_then_close_1000_fails(self):
        """Real transport, Grok P1 case 2: ordinary reset -> trailing delta ->
        clean close. The last data frame is not a reset — must fail."""

        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "普通分段", reset=True)
                        await server.send_success(ws, "还没结束")
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(ConfuciusProtocolError, match="missing EOF"):
            session.finish()

    def test_malformed_payload_fails(self):
        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "最终", reset=True)
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1
                if server.binary_frames == 1:
                    await ws.send("not-json{{{")

        server = _LoopbackServer(_scenario).start()
        provider = _provider_for(server)
        session = provider.start_realtime_session(hotwords=[])
        session.push_audio((np.ones(64, dtype="<f4") * 0.01).tobytes())
        with pytest.raises(RuntimeError, match="malformed"):
            session.finish()

    def test_file_feed_over_64_frames_with_slow_consumer(self, tmp_path):
        """>64 frames (queue cap is 6), server consumes slower than the
        producer pushes: bounded backpressure must carry the feed to a normal
        completion inside the total deadline — no overflow, no truncation."""
        import asyncio

        n_frames = 100  # 16.0 s of audio at the 160 ms step
        samples_per_frame = int(16000 * REALTIME_CHUNK_SIZE_SEC)
        pcm = struct.pack(f"<{n_frames * samples_per_frame}h", *([0] * n_frames * samples_per_frame))
        wav_path = tmp_path / "long.wav"
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(pcm)

        async def _scenario(server: _LoopbackServer, ws):
            async for message in ws:
                if isinstance(message, str):
                    if message == EOS_MESSAGE:
                        await server.send_success(ws, "完成", reset=True)
                        await ws.close(code=1000)
                        return
                    continue
                server.binary_frames += 1
                await asyncio.sleep(0.02)  # consume far slower than the push rate

        server = _LoopbackServer(_scenario).start()
        provider = ConfuciusASRProvider(
            f"ws://127.0.0.1:{server.port}/asr_stream_api_v1",
            api_key="loopback-key",
            timeout_s=10.0,
        )
        started = time.monotonic()
        result = provider.transcribe_file(wav_path, hotwords=[])
        elapsed = time.monotonic() - started
        assert result.text == "完成"
        assert server.binary_frames == n_frames  # every frame arrived, in order
        audio_seconds = n_frames * REALTIME_CHUNK_SIZE_SEC
        assert elapsed < audio_seconds + 10.0  # inside the declared deadline
