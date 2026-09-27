"""Official Qwen3-ASR streaming server.

Uses Qwen3ASRModel.LLM plus streaming_transcribe, exposed as the same
/api/start /api/chunk /api/finish endpoints as qwen_asr.cli.demo_streaming.
max_model_len is capped so the 8GB GPU still has room for the KV cache.

Model and Flask dependencies are imported lazily inside :func:`main` so unit
tests can import this module (and its pure helpers) without vLLM installed.
"""

from __future__ import annotations

import argparse

# Hotword/context hints are sent whole; the model prompt budget is enforced by
# the caller (compose side), not by a blind character cut here.
MAX_CONTEXT_CHARS = 4000

# Populated by main() once the real model is loaded; None in unit tests.
demo = None  # type: ignore[assignment]


def _force_language(value: str) -> str | None:
    token = value.strip()
    if not token or token.lower() in {"auto", "detect"}:
        return None
    mapped = {"zh": "Chinese", "en": "English", "chinese": "Chinese", "english": "English"}
    return mapped.get(token.lower(), token)


def _resolve_context(raw: object) -> str:
    """Keep the full hotword/context payload within the prompt budget.

    Truncation (if ever needed) happens at the end of the free-form text;
    callers put hotwords first so they survive the budget.
    """
    text = str(raw or "").strip()
    if len(text) > MAX_CONTEXT_CHARS:
        return text[:MAX_CONTEXT_CHARS]
    return text


def _api_start():
    """Honor the language and context Recordian sends with /api/start.

    Without a forced language the streaming decoder emits ``language None``
    and the text stays empty until the separate full-file request.
    """
    import time
    import uuid

    from flask import jsonify, request

    body = request.get_json(silent=True) or {}
    language = _force_language(str(body.get("language") or "Chinese"))
    context = _resolve_context(body.get("context"))
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


def build_parser() -> argparse.ArgumentParser:
    import os

    parser = argparse.ArgumentParser(description="Qwen3-ASR streaming HTTP server (vLLM backend)")
    parser.add_argument(
        "--model",
        default=os.environ.get("ASR_MODEL_PATH", "Qwen/Qwen3-ASR-0.6B"),
        help="Model path or repo id (env: ASR_MODEL_PATH)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default loopback)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.80)
    parser.add_argument("--max-model-len", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    return parser


def main(argv: list[str] | None = None) -> None:
    global demo  # noqa: PLW0603

    args = build_parser().parse_args(argv)

    import qwen_asr.cli.demo_streaming as _demo
    from flask import jsonify, request
    from qwen_asr import Qwen3ASRModel

    demo = _demo
    model = args.model

    @demo.app.get("/v1/models")
    def list_models():
        return jsonify({"object": "list", "data": [{"id": model, "object": "model"}]})

    @demo.app.post("/v1/audio/transcriptions")
    def transcribe_upload():
        import tempfile
        from pathlib import Path

        upload = request.files.get("file")
        if upload is None:
            return jsonify({"error": "file required"}), 400
        language = _force_language(request.form.get("language") or "")
        prompt = _resolve_context(request.form.get("prompt"))
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
            "model": model,
        })

    demo.app.view_functions["api_start"] = _api_start

    demo.UNFIXED_CHUNK_NUM = 4
    demo.UNFIXED_TOKEN_NUM = 5
    demo.CHUNK_SIZE_SEC = 0.5
    demo.asr = Qwen3ASRModel.LLM(
        model=model,
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_model_len=args.max_model_len,
        max_new_tokens=args.max_new_tokens,
    )
    print("Model loaded.", flush=True)
    demo.app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
