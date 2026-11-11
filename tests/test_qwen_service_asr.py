"""Tests for the QwenServiceASRProvider client (no network, no GPU).

The transport is a fake requests.Session: each test queues the responses
the service would give and inspects the requests the provider made.
"""
from __future__ import annotations

import base64
import io
import json
import struct
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.posts: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> _FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if not self._responses:
            raise AssertionError("unexpected extra POST")
        return self._responses.pop(0)


def _provider(session: _FakeSession, **overrides):
    from recordian.providers.qwen_service_asr import QwenServiceASRProvider

    kwargs = {"endpoint": "http://127.0.0.1:8323", "token_file": "", "context": "CTX", "http_session": session}
    kwargs.update(overrides)
    return QwenServiceASRProvider(**kwargs)


def _ok(text: str = "你好世界", **extra) -> _FakeResponse:
    payload = {"text": text, "detected_language": "Chinese", "model": "m", "decode_elapsed_s": 0.5}
    payload.update(extra)
    return _FakeResponse(200, payload)


def _write_wav(path: Path, *, seconds: float = 0.5, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


def test_transcribe_file_posts_wav_and_maps_result(tmp_path: Path) -> None:
    wav_path = tmp_path / "clip.wav"
    _write_wav(wav_path)
    session = _FakeSession([_ok()])
    provider = _provider(session)

    result = provider.transcribe_file(wav_path, hotwords=["jev"])

    assert result.text == "你好世界"
    assert result.detected_language == "Chinese"
    assert result.metadata["decode_elapsed_s"] == 0.5
    assert len(session.posts) == 1
    posted = session.posts[0]
    assert posted["url"] == "http://127.0.0.1:8323/transcribe"
    assert base64.b64decode(posted["json"]["audio_base64"]) == wav_path.read_bytes()
    assert posted["json"]["hotwords"] == ["jev"]
    assert posted["json"]["context"] == "CTX"
    assert posted["headers"] == {}  # no token file configured


def test_transcribe_file_sends_bearer_when_token_file_exists(tmp_path: Path) -> None:
    token_path = tmp_path / "tok.txt"
    token_path.write_text("test-bearer-value\n", encoding="utf-8")
    token_path.chmod(0o600)
    wav_path = tmp_path / "clip.wav"
    _write_wav(wav_path)
    session = _FakeSession([_ok()])
    provider = _provider(session, token_file=str(token_path))

    provider.transcribe_file(wav_path, hotwords=[])

    expected = "Bearer " + token_path.read_text(encoding="utf-8").strip()
    assert session.posts[0]["headers"] == {"Authorization": expected}


def test_transcribe_file_missing_file_raises(tmp_path: Path) -> None:
    provider = _provider(_FakeSession([]))
    with pytest.raises(FileNotFoundError):
        provider.transcribe_file(tmp_path / "nope.wav", hotwords=[])


def test_transcribe_retries_on_busy_then_succeeds(tmp_path: Path) -> None:
    wav_path = tmp_path / "clip.wav"
    _write_wav(wav_path)
    session = _FakeSession([_FakeResponse(429, {"error": "model busy"}), _ok("忙后成功")])
    provider = _provider(session)

    result = provider.transcribe_file(wav_path, hotwords=[])

    assert result.text == "忙后成功"
    assert len(session.posts) == 2


def test_transcribe_http_error_raises_runtime_error(tmp_path: Path) -> None:
    wav_path = tmp_path / "clip.wav"
    _write_wav(wav_path)
    provider = _provider(_FakeSession([_FakeResponse(500, {"error": "transcription_failed"})]))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        provider.transcribe_file(wav_path, hotwords=[])


def test_f32le_to_pcm16le_clips_and_zeroes_nonfinite() -> None:
    from recordian.providers.qwen_service_asr import f32le_to_pcm16le

    raw = struct.pack("<5f", 0.0, 0.5, -1.0, 2.0, float("nan"))
    pcm = f32le_to_pcm16le(raw)
    values = struct.unpack("<5h", pcm)
    assert values[0] == 0
    assert values[1] == int(0.5 * 32767)
    assert values[2] == -32767
    assert values[3] == 32767
    assert values[4] == 0
    with pytest.raises(ValueError):
        f32le_to_pcm16le(b"\x00\x00")


def _f32_silence(seconds: float) -> bytes:
    return b"\x00\x00\x00\x00" * int(16000 * seconds)


def test_session_finish_decodes_buffered_audio_once() -> None:
    session = _FakeSession([_ok("整段文本")])
    provider = _provider(session)
    realtime = provider.start_realtime_session(hotwords=["jev"])

    assert realtime.push_audio(_f32_silence(1.0)) == {"text": ""}
    result = realtime.finish()

    assert result.text == "整段文本"
    assert len(session.posts) == 1
    wav_bytes = base64.b64decode(session.posts[0]["json"]["audio_base64"])
    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 16000


def test_session_finish_chunks_long_presses(monkeypatch: pytest.MonkeyPatch) -> None:
    import recordian.providers.qwen_service_asr as module

    monkeypatch.setattr(module, "_FINISH_CHUNK_S", 2.0)
    session = _FakeSession([_ok("甲"), _ok("乙"), _ok("丙")])
    provider = _provider(session)
    realtime = provider.start_realtime_session(hotwords=[])
    realtime.push_audio(_f32_silence(5.0))

    result = realtime.finish()

    assert result.text == "甲乙丙"
    assert len(session.posts) == 3


def test_session_finish_after_cancel_raises() -> None:
    provider = _provider(_FakeSession([]))
    realtime = provider.start_realtime_session(hotwords=[])
    realtime.push_audio(_f32_silence(0.5))
    realtime.cancel()
    with pytest.raises(RuntimeError, match="cancelled"):
        realtime.finish()


def test_session_cancel_during_finish_stops_remaining_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    import recordian.providers.qwen_service_asr as module

    monkeypatch.setattr(module, "_FINISH_CHUNK_S", 2.0)
    session = _FakeSession([_ok("甲"), _ok("乙"), _ok("丙")])
    provider = _provider(session)
    realtime = provider.start_realtime_session(hotwords=[])
    realtime.push_audio(_f32_silence(5.0))
    original_post = provider._post_transcribe

    def post_then_cancel(*args, **kwargs):
        data = original_post(*args, **kwargs)
        realtime.cancel()  # 第一块解码完成后用户取消
        return data

    monkeypatch.setattr(provider, "_post_transcribe", post_then_cancel)

    with pytest.raises(RuntimeError, match="cancelled"):
        realtime.finish()

    assert len(session.posts) == 1  # 剩余两块不再占用服务


def test_session_preview_updates_partial_text() -> None:
    session = _FakeSession([_ok("预览文本"), _ok("最终文本")])
    provider = _provider(
        session,
        preview_interval_s=0.0,
        preview_min_new_s=0.1,
        preview_max_audio_s=30.0,
        preview_timeout_s=5.0,
    )
    realtime = provider.start_realtime_session(hotwords=[])
    realtime.push_audio(_f32_silence(1.0))

    deadline = time.monotonic() + 5.0
    seen = ""
    while time.monotonic() < deadline:
        seen = realtime.push_audio(b"")["text"]
        if seen == "预览文本":
            break
        time.sleep(0.05)
    assert seen == "预览文本"

    result = realtime.finish()
    assert result.text == "最终文本"
    assert len(session.posts) == 2


def test_create_provider_qwen_service_branch() -> None:
    from recordian.linux_dictate import create_provider
    from recordian.providers.qwen_service_asr import QwenServiceASRProvider

    args = SimpleNamespace(
        asr_provider="qwen-asr-service",
        qwen_service_endpoint="http://127.0.0.1:9999",
        asr_timeout_s=60,
        asr_context="订单上下文",
        asr_context_preset="",
    )
    provider = create_provider(args)
    assert isinstance(provider, QwenServiceASRProvider)
    assert provider.endpoint == "http://127.0.0.1:9999"
    assert provider.timeout_s == 120.0  # floored for whole-segment decode
    assert provider.context == "订单上下文"


def test_runtime_config_accepts_qwen_service_provider() -> None:
    from recordian.runtime_config import normalize_runtime_config

    normalized = normalize_runtime_config({"asr_provider": "qwen-asr-service"}, include_sound_defaults=False)
    assert normalized["asr_provider"] == "qwen-asr-service"
    assert normalized["qwen_service_endpoint"] == "http://127.0.0.1:8323"

    custom = normalize_runtime_config(
        {"asr_provider": "qwen-asr-service", "qwen_service_endpoint": " http://127.0.0.1:7777 "},
        include_sound_defaults=False,
    )
    assert custom["qwen_service_endpoint"] == "http://127.0.0.1:7777"


def test_provider_registered_in_lazy_registry() -> None:
    from recordian.providers import QwenServiceASRProvider

    assert QwenServiceASRProvider.__name__ == "QwenServiceASRProvider"
    provider = QwenServiceASRProvider(token_file="")
    assert provider.provider_name == "qwen-asr-service"
    assert provider.capabilities.supports_realtime is True
    assert json.dumps(provider.provider_name)  # plain value, no surprises
