from __future__ import annotations

import base64
from types import SimpleNamespace

from server import asr_server


def test_asr_server_transcribe_applies_context_hotwords_and_language() -> None:
    calls: list[dict[str, object]] = []

    class _FakeModel:
        def transcribe(self, *, audio, context, language, return_time_stamps):  # noqa: ANN001
            calls.append(
                {
                    "audio": audio,
                    "context": context,
                    "language": language,
                    "return_time_stamps": return_time_stamps,
                }
            )
            return [SimpleNamespace(text="OpenClaw 会议开始")]

    asr_server.asr_model = _FakeModel()
    asr_server.model_name = "fake-qwen"

    client = asr_server.app.test_client()
    response = client.post(
        "/transcribe",
        json={
            "audio_base64": base64.b64encode(b"RIFFdemo").decode("ascii"),
            "hotwords": ["OpenClaw", "会议", "OpenClaw"],
            "context": "固定上下文",
            "language": "zh",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["text"] == "OpenClaw 会议开始"
    assert payload["applied_hotwords"] == ["OpenClaw", "会议"]
    assert payload["applied_context"] == "固定上下文\n热词: OpenClaw, 会议"
    assert payload["requested_language"] == "zh"
    assert calls == [
        {
            "audio": calls[0]["audio"],
            "context": "固定上下文\n热词: OpenClaw, 会议",
            "language": "zh",
            "return_time_stamps": False,
        }
    ]
