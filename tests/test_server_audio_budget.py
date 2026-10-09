from __future__ import annotations

import base64
import io
import wave
from types import SimpleNamespace

from server import asr_server


def test_wav_declared_duration_is_checked_before_inference(monkeypatch):
    calls = []
    monkeypatch.setattr(asr_server, 'asr_model', SimpleNamespace(transcribe=lambda **kw: calls.append(kw) or [SimpleNamespace(text='synthetic')]))
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\0\0' * 968000)
    response = asr_server.app.test_client().post('/transcribe', json={'audio_base64': base64.b64encode(audio.getvalue()).decode()})
    assert response.status_code == 400
    assert calls == []
