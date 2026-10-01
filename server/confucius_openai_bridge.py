"""OpenAI-compatible /v1/audio/transcriptions bridge -> Confucius4-R2T2 ws stream.

Hermes STT (tools/transcription_tools.py) only speaks the OpenAI HTTP contract
POST /v1/audio/transcriptions. Confucius server only speaks a YOUDAO-style
WebSocket stream. This bridge translates: multipart wav -> PCM16 16k mono frames
-> ws://127.0.0.1:8321/asr_stream_api_v1, aggregates deltas, returns {"text": ...}.

Auth: Bearer header is ignored (local service); Confucius ws auth uses the token
file. Keep on loopback; single-user service anyway (CLOSE 4429 when busy).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import uuid

import flask
from flask import Flask, jsonify, request
from websockets.asyncio.client import connect

WS_URL = "ws://127.0.0.1:8321/asr_stream_api_v1"
TOKEN_FILE = "/home/zz8011/.config/recordian/confucius_server_token.txt"
SAMPLE_RATE = 16000
EOS = "YOUDAO_ONETIME_ASR_STREAM_EOS"
FRAME_BYTES = SAMPLE_RATE * 160 // 1000 * 2  # 160ms PCM16 mono

app = Flask(__name__)


def _pcm16_mono(wav_bytes: bytes) -> bytes:
    """ffmpeg: any container -> raw PCM16 mono 16k. Fail loudly on empty."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0",
         "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=wav_bytes, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError(f"ffmpeg decode failed: {proc.stderr.decode()[:200]}")
    return proc.stdout


async def _transcribe(pcm: bytes, language: str) -> str:
    token = open(TOKEN_FILE).read().strip()
    header = {
        "requestId": uuid.uuid4().hex,
        "secret_key": token,
        "language": language or "auto",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
    }
    parts: list[str] = []
    async with connect(WS_URL, max_size=2**22) as ws:
        await ws.send(json.dumps(header))
        await ws.recv()  # {"status":"connected",...}
        for i in range(0, len(pcm), FRAME_BYTES):
            await ws.send(pcm[i:i + FRAME_BYTES])
            # ponytail: server queue is 32 frames (5.1s) and the official client
            # paces at realtime; blasting fast = 1008 backpressure. Ceiling: if
            # server ever grows a credit/budget feedback msg, drop this sleep.
            await asyncio.sleep(FRAME_BYTES / (SAMPLE_RATE * 2))
        await ws.send(EOS)
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("status") != "success":
                continue
            text = (msg.get("msg") or {}).get("text") or ""
            if text:
                parts.append(text)
            if (msg.get("msg") or {}).get("reset"):
                break
    return "".join(parts)


@app.get("/v1/models")
def models():
    return jsonify({"object": "list",
                    "data": [{"id": "confucius4-r2t2", "object": "model"}]})


@app.post("/v1/audio/transcriptions")
def transcriptions():
    try:
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": {"message": "missing file field"}}), 400
        language = request.form.get("language") or "Chinese"
        # Confucius validates against its own list (Chinese/English/...), not ISO codes
        lang_map = {"zh": "Chinese", "zh-cn": "Chinese", "en": "English"}
        language = lang_map.get(language.lower(), language)
        pcm = _pcm16_mono(file.read())
        text = asyncio.run(_transcribe(pcm, language))
        return jsonify({"text": text})
    except Exception as exc:  # surface as OpenAI-style error for the SDK
        app.logger.exception("transcription failed")
        return jsonify({"error": {"message": str(exc), "type": type(exc).__name__}}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8322, threaded=True)
