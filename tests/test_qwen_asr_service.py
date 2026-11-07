"""Tests for server/qwen_asr_service.py (Flask test client, fake provider).

The service must stay a thin host around QwenASRProvider: these tests pin
the HTTP contract (health, transcribe, busy slot, auth) without a GPU.
"""
from __future__ import annotations

import base64
import io
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import qwen_asr_service as service  # noqa: E402


class _FakeProvider:
    model_name = "fake-qwen"

    def __init__(self) -> None:
        self.context = ""
        self.calls: list[dict] = []

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]):
        self.calls.append(
            {"hotwords": list(hotwords), "context": self.context, "bytes": Path(wav_path).stat().st_size}
        )
        return SimpleNamespace(text="识别文本", detected_language="Chinese", model_name="fake-qwen")


def _wav_b64(seconds: float = 0.5) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture()
def client():
    fake = _FakeProvider()
    service.init_provider(fake)
    service._AUTH.bearer = ""
    with service.app.test_client() as test_client:
        yield test_client, fake
    service.init_provider(None)


def test_health_ok_after_init(client) -> None:
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["provider"] == "qwen-asr-service"


def test_health_503_without_provider() -> None:
    service.init_provider(None)
    with service.app.test_client() as test_client:
        assert test_client.get("/health").status_code == 503


def test_transcribe_roundtrip_passes_context_and_hotwords(client) -> None:
    test_client, fake = client
    response = test_client.post(
        "/transcribe",
        json={"audio_base64": _wav_b64(), "hotwords": ["jev"], "context": "CTX"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["text"] == "识别文本"
    assert payload["detected_language"] == "Chinese"
    assert payload["decode_elapsed_s"] >= 0
    assert fake.calls == [{"hotwords": ["jev"], "context": "CTX", "bytes": len(base64.b64decode(_wav_b64()))}]


def test_transcribe_rejects_bad_payloads(client) -> None:
    test_client, _ = client
    assert test_client.post("/transcribe", json={}).status_code == 400
    assert test_client.post("/transcribe", json={"audio_base64": "!!!"}).status_code == 400
    not_wav = base64.b64encode(b"not a wav").decode("ascii")
    assert test_client.post("/transcribe", json={"audio_base64": not_wav}).status_code == 400
    assert test_client.post("/transcribe", json={"audio_base64": _wav_b64(), "hotwords": "jev"}).status_code == 400


def test_transcribe_busy_slot_returns_429(client) -> None:
    test_client, _ = client
    assert service._request_slot.acquire(blocking=False)
    try:
        response = test_client.post("/transcribe", json={"audio_base64": _wav_b64()})
        assert response.status_code == 429
    finally:
        service._request_slot.release()


def test_bearer_auth_enforced_when_set(client) -> None:
    test_client, _ = client
    service._AUTH.bearer = "expected" + "-value"
    try:
        assert test_client.get("/health").status_code == 401
        ok = test_client.get("/health", headers={"Authorization": "Bearer " + service._AUTH.bearer})
        assert ok.status_code == 200
        bad = test_client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
    finally:
        service._AUTH.bearer = ""


def test_build_provider_uses_harness_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _StubProvider:
        def __init__(self, model_name: str, **kwargs) -> None:
            captured["model_name"] = model_name
            captured.update(kwargs)

        def _lazy_load(self) -> None:
            captured["loaded"] = True

    import recordian.providers.qwen_asr as qwen_module

    monkeypatch.setattr(qwen_module, "QwenASRProvider", _StubProvider)
    provider = service.build_provider("/models/Qwen3-ASR-1.7B", "cuda:0", "bfloat16", 1024)
    assert isinstance(provider, _StubProvider)
    assert captured == {
        "model_name": "/models/Qwen3-ASR-1.7B",
        "device": "cuda:0",
        "dtype": "bfloat16",
        "language": None,
        "max_new_tokens": 1024,
        "context": "",
        "loaded": True,
    }
