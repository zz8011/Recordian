from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from recordian.audio import write_wav_mono_f32
from recordian.providers.http_cloud import HttpCloudProvider, _group_hotword_variants

# Skip tests if requests is not installed
pytest.importorskip("requests")


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeStreamingResponse(_FakeResponse):
    def __init__(self, lines: list[str]) -> None:
        super().__init__({})
        self._lines = lines

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._lines:
            yield line.encode("utf-8")


class _FakeRequestsSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._texts = iter(["你", "你好"])

    def post(self, url: str, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url.endswith("/api/start"):
            return _FakeResponse({"session_id": "demo-session"})
        if url.endswith("/api/chunk"):
            return _FakeResponse({"text": next(self._texts)})
        if url.endswith("/api/finish"):
            return _FakeResponse({"text": "你好", "model": "qwen3-asr-1.7b"})
        raise AssertionError(url)

    def delete(self, url: str, **kwargs):
        self.calls.append(("delete", url, kwargs))
        return _FakeResponse({"ok": True})


def test_http_cloud_provider_transcribe(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider("http://localhost:9999/asr", api_key="k")

    payload = {
        "text": "你好 world",
        "confidence": 0.92,
        "model": "cloud-asr-v1",
        "language": "zh",
        "segments": [{"text": "你好 world", "start": 0, "end": 1400}],
    }
    with patch("requests.post", return_value=_FakeResponse(payload)):
        result = provider.transcribe_file(wav_path, hotwords=["你好"])

    assert result.text == "你好 world"
    assert result.confidence == 0.92
    assert result.model_name == "cloud-asr-v1"
    assert result.detected_language == "zh"
    assert result.timestamps == [(0, 1400)]
    assert len(result.segments) == 1
    assert result.english_ratio > 0.0


def test_http_cloud_provider_resolves_openai_model_name_from_models_endpoint(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="Qwen/Qwen3-ASR-0.6B",
    )

    post_payload = {"text": "转录成功", "confidence": 0.95, "model": "Qwen3-ASR-0.6B"}
    with (
        patch(
            "requests.get",
            return_value=_FakeResponse({"data": [{"id": "Qwen3-ASR-0.6B"}]}),
        ),
        patch("requests.post", return_value=_FakeResponse(post_payload)) as mock_post,
    ):
        result = provider.transcribe_file(wav_path, hotwords=[])

    assert result.text == "转录成功"
    assert result.model_name == "Qwen3-ASR-0.6B"
    assert mock_post.call_args.kwargs["data"]["model"] == "Qwen3-ASR-0.6B"


def test_http_cloud_provider_falls_back_to_single_openai_model(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="custom-model-name",
    )

    post_payload = {"text": "fallback ok", "confidence": 0.88, "model": "Qwen3-ASR-0.6B"}
    with (
        patch(
            "requests.get",
            return_value=_FakeResponse({"data": [{"id": "Qwen3-ASR-0.6B"}]}),
        ),
        patch("requests.post", return_value=_FakeResponse(post_payload)) as mock_post,
    ):
        provider.transcribe_file(wav_path, hotwords=[])

    assert mock_post.call_args.kwargs["data"]["model"] == "Qwen3-ASR-0.6B"


def test_estimate_english_ratio_in_base() -> None:
    from recordian.providers.base import _estimate_english_ratio
    assert _estimate_english_ratio("hello world") == 1.0
    assert _estimate_english_ratio("你好世界") == 0.0
    assert _estimate_english_ratio("") == 0.0


def test_http_cloud_provider_transcribe_stream_parses_sse_chunks(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="Qwen3-ASR-0.6B",
        language="zh",
    )

    streaming = _FakeStreamingResponse(
        [
            'data: {"choices":[{"delta":{"content":"language"}}]}',
            'data: {"choices":[{"delta":{"content":" None<asr_text>你"}}]}',
            'data: {"choices":[{"delta":{"content":"好"}}]}',
            'data: {"choices":[{"delta":{"content":"</asr_text>"}}]}',
            'data: [DONE]',
        ]
    )
    with (
        patch("requests.get", return_value=_FakeResponse({"data": [{"id": "Qwen3-ASR-0.6B"}]})),
        patch("requests.post", return_value=streaming),
    ):
        chunks = list(provider.transcribe_file_stream(wav_path, hotwords=[]))

    assert chunks == ["你", "好"]


def test_http_cloud_provider_realtime_session_roundtrip() -> None:
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="qwen3-asr-1.7b",
        language="zh",
        realtime_endpoint="http://127.0.0.1:40002",
    )

    fake_session = _FakeRequestsSession()
    with (
        patch("requests.get", return_value=_FakeResponse({"data": [{"id": "qwen3-asr-1.7b"}]})),
        patch("requests.Session", return_value=fake_session),
    ):
        session = provider.start_realtime_session(hotwords=["露露"])
        update = session.push_audio(b"\x00\x00\x00\x00")
        result = session.finish()

    assert update["text"] == "你"
    assert result.text == "你好"
    assert result.model_name == "qwen3-asr-1.7b"
    assert fake_session.calls[0][1].endswith("/api/start")
    assert fake_session.calls[1][1].endswith("/api/chunk")
    assert fake_session.calls[2][1].endswith("/api/finish")
    assert result.metadata["realtime"] is True


def test_http_cloud_provider_resolves_realtime_model_name_case_insensitively() -> None:
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="Qwen3-ASR-0.6B",
        language="zh",
        realtime_endpoint="http://127.0.0.1:40002",
    )

    fake_session = _FakeRequestsSession()
    with (
        patch("requests.get", return_value=_FakeResponse({"data": [{"id": "qwen3-asr-1.7b"}]})),
        patch("requests.Session", return_value=fake_session),
    ):
        session = provider.start_realtime_session(hotwords=[])
        session.cancel()

    start_call = fake_session.calls[0]
    payload = start_call[2]["json"]
    assert payload["model"] == "qwen3-asr-1.7b"


def test_http_cloud_provider_capabilities_reflect_endpoint_types() -> None:
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        realtime_endpoint="http://127.0.0.1:40002",
    )

    capabilities = provider.capabilities

    assert capabilities.supports_hotwords is True
    assert capabilities.supports_context is True
    assert capabilities.supports_language_hint is True
    assert capabilities.supports_file_streaming is True
    assert capabilities.supports_realtime is True


def test_http_cloud_provider_uses_composed_context_for_openai_prompt(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="Qwen/Qwen3-ASR-0.6B",
        context="固定上下文",
    )

    post_payload = {"text": "转录成功", "confidence": 0.95, "model": "Qwen3-ASR-0.6B"}
    with (
        patch(
            "requests.get",
            return_value=_FakeResponse({"data": [{"id": "Qwen3-ASR-0.6B"}]}),
        ),
        patch("requests.post", return_value=_FakeResponse(post_payload)) as mock_post,
    ):
        provider.transcribe_file(wav_path, hotwords=["OpenClaw", "小二", "OpenClaw"])

    prompt = mock_post.call_args.kwargs["data"]["prompt"]
    assert "固定上下文" in prompt
    assert "请优先按原样输出以下专有名词/产品名" in prompt
    assert "- OpenClaw" in prompt
    assert "- 小二" in prompt


def test_http_cloud_provider_uses_composed_context_for_generic_json_transcribe(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/transcribe",
        context="固定上下文",
        language="zh",
    )

    post_payload = {"text": "转录成功", "confidence": 0.95, "model": "cloud-asr"}
    with patch("requests.post", return_value=_FakeResponse(post_payload)) as mock_post:
        provider.transcribe_file(wav_path, hotwords=["OpenClaw", " 小二 ", "OpenClaw"])

    payload = mock_post.call_args.kwargs["json"]
    assert payload["hotwords"] == ["OpenClaw", "小二"]
    assert payload["context"] == "固定上下文"
    assert payload["language"] == "zh"


def test_http_cloud_provider_uses_composed_context_for_realtime_session() -> None:
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="qwen3-asr-1.7b",
        language="zh",
        context="固定上下文",
        realtime_endpoint="http://127.0.0.1:40002",
    )

    fake_session = _FakeRequestsSession()
    with (
        patch("requests.get", return_value=_FakeResponse({"data": [{"id": "qwen3-asr-1.7b"}]})),
        patch("requests.Session", return_value=fake_session),
    ):
        provider.start_realtime_session(hotwords=["露露", "露露"])

    payload = fake_session.calls[0][2]["json"]
    assert "固定上下文" in payload["context"]
    assert "- 露露" in payload["context"]


def test_group_hotword_variants_prefers_readable_canonical_spelling() -> None:
    groups = _group_hotword_variants(["OPENCLAW", "OpenClaw", "openclaw", "open claw", "Codex", "Claude"])

    assert groups[0] == ("OpenClaw", ["OPENCLAW", "openclaw", "open claw"])
    assert groups[1] == ("Codex", [])
    assert groups[2] == ("Claude", [])
