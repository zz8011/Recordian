"""Official Qwen3-ASR streaming server.

Uses Qwen3ASRModel.LLM plus streaming_transcribe, exposed as the same
/api/start /api/chunk /api/finish endpoints as qwen_asr.cli.demo_streaming.
max_model_len is capped so the 8GB GPU still has room for the KV cache.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import qwen_asr.cli.demo_streaming as demo
from flask import jsonify, request
from qwen_asr import Qwen3ASRModel

MODEL = "/home/zz8011/文档/Develop/Recordian/models/Qwen3-ASR-0.6B"


def _force_language(value: str) -> str | None:
    token = value.strip()
    if not token or token.lower() in {"auto", "detect"}:
        return None
    mapped = {"zh": "Chinese", "en": "English", "chinese": "Chinese", "english": "English"}
    return mapped.get(token.lower(), token)


def _api_start():
    """Honor the language and context Recordian sends with /api/start.

    Without a forced language the streaming decoder emits ``language None``
    and the text stays empty until the separate full-file request.
    """
    import time
    import uuid

    body = request.get_json(silent=True) or {}
    language = _force_language(str(body.get("language") or "Chinese"))
    context = str(body.get("context") or "").strip()[:80]
    state = demo.asr.init_streaming_state(
        context=context,
        language=language,
        unfixed_chunk_num=int(demo.UNFIXED_CHUNK_NUM),
        unfixed_token_num=int(demo.UNFIXED_TOKEN_NUM),
        chunk_size_sec=float(demo.CHUNK_SIZE_SEC),
    )
    now = time.time()
    session_id = uuid.uuid4().hex
    demo.SESSIONS[session_id] = demo.Session(state=state, created_at=now, last_seen=now)
    return jsonify({"session_id": session_id})


demo.app.view_functions["api_start"] = _api_start


@demo.app.get("/v1/models")
def list_models():
    return jsonify({"object": "list", "data": [{"id": MODEL, "object": "model"}]})


@demo.app.post("/v1/audio/transcriptions")
def transcribe_upload():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "file required"}), 400
    language = _force_language(request.form.get("language") or "")
    prompt = (request.form.get("prompt") or "").strip()
    suffix = Path(upload.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        upload.save(handle.name)
        audio_path = handle.name
    try:
        results = demo.asr.transcribe(
            audio=audio_path,
            context=prompt,
            language=language,
            return_time_stamps=False,
        )
    finally:
        Path(audio_path).unlink(missing_ok=True)
    result = results[0]
    return jsonify({
        "text": result.text or "",
        "language": getattr(result, "language", "") or "",
        "model": MODEL,
    })


def main() -> None:
    demo.UNFIXED_CHUNK_NUM = 4
    demo.UNFIXED_TOKEN_NUM = 5
    demo.CHUNK_SIZE_SEC = 0.5
    demo.asr = Qwen3ASRModel.LLM(
        model=MODEL,
        gpu_memory_utilization=0.80,
        max_model_len=2048,
        max_new_tokens=64,
    )
    print("Model loaded.", flush=True)
    demo.app.run(host="127.0.0.1", port=8000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
