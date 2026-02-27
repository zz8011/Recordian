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


def test_estimate_english_ratio_in_base() -> None:
    from recordian.providers.base import _estimate_english_ratio
    assert _estimate_english_ratio("hello world") == 1.0
    assert _estimate_english_ratio("你好世界") == 0.0
    assert _estimate_english_ratio("") == 0.0
