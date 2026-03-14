from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from recordian.audio import write_wav_mono_f32
from recordian.providers.http_cloud import HttpCloudProvider

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


def test_http_cloud_provider_transcribe(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider("http://localhost:9999/asr", api_key="k")

    payload = {"text": "你好 world", "confidence": 0.92, "model": "cloud-asr-v1"}
    with patch("requests.post", return_value=_FakeResponse(payload)):
        result = provider.transcribe_file(wav_path, hotwords=["你好"])

    assert result.text == "你好 world"
    assert result.confidence == 0.92
    assert result.model_name == "cloud-asr-v1"
    assert result.english_ratio > 0.0


def test_http_cloud_provider_resolves_openai_model_name_from_models_endpoint(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="Qwen/Qwen3-ASR-1.7B",
    )

    post_payload = {"text": "转录成功", "confidence": 0.95, "model": "Qwen3-ASR-1.7B"}
    with (
        patch(
            "requests.get",
            return_value=_FakeResponse({"data": [{"id": "Qwen3-ASR-1.7B"}]}),
        ),
        patch("requests.post", return_value=_FakeResponse(post_payload)) as mock_post,
    ):
        result = provider.transcribe_file(wav_path, hotwords=[])

    assert result.text == "转录成功"
    assert result.model_name == "Qwen3-ASR-1.7B"
    assert mock_post.call_args.kwargs["data"]["model"] == "Qwen3-ASR-1.7B"


def test_http_cloud_provider_falls_back_to_single_openai_model(tmp_path: Path) -> None:
    wav_path = tmp_path / "demo.wav"
    write_wav_mono_f32(wav_path, [0.0] * 1600, sample_rate=16000)
    provider = HttpCloudProvider(
        "http://127.0.0.1:8000/v1/audio/transcriptions",
        model_name="custom-model-name",
    )

    post_payload = {"text": "fallback ok", "confidence": 0.88, "model": "Qwen3-ASR-1.7B"}
    with (
        patch(
            "requests.get",
            return_value=_FakeResponse({"data": [{"id": "Qwen3-ASR-1.7B"}]}),
        ),
        patch("requests.post", return_value=_FakeResponse(post_payload)) as mock_post,
    ):
        provider.transcribe_file(wav_path, hotwords=[])

    assert mock_post.call_args.kwargs["data"]["model"] == "Qwen3-ASR-1.7B"


def test_estimate_english_ratio_in_base() -> None:
    from recordian.providers.base import _estimate_english_ratio
    assert _estimate_english_ratio("hello world") == 1.0
    assert _estimate_english_ratio("你好世界") == 0.0
    assert _estimate_english_ratio("") == 0.0
