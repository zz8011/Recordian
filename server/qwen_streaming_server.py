"""Official Qwen3-ASR streaming server.

Uses Qwen3ASRModel.LLM plus streaming_transcribe, exposed as the same
/api/start /api/chunk /api/finish endpoints as qwen_asr.cli.demo_streaming.
max_model_len is capped so the 8GB GPU still has room for the KV cache.

Model and Flask dependencies are imported lazily inside :func:`main` so unit
tests can import this module (and its pure helpers) without vLLM installed.
"""

from __future__ import annotations

import argparse

# Keep hotwords first; truncate only the trailing context past this server budget.
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

    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({'error': 'request must be an object'}), 400
    demo._gc_sessions()
    if len(demo.SESSIONS) >= 8:
        return jsonify({'error': 'too many active sessions'}), 429
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
    demo.SESSIONS[session_id].recordian_samples = 0
    return jsonify({"session_id": session_id})


def _api_chunk():
    import numpy as np
    from flask import jsonify, request

    session_id = request.args.get('session_id', '')
    session = demo._get_session(session_id)
    if session is None:
        return jsonify({'error': 'invalid session_id'}), 400
    if request.mimetype != 'application/octet-stream':
        return jsonify({'error': 'expect application/octet-stream'}), 400
    raw = request.get_data(cache=False)
    if len(raw) % 4:
        return jsonify({'error': 'float32 bytes length not multiple of 4'}), 400
    samples = np.frombuffer(raw, dtype='<f4')
    count = getattr(session, 'recordian_samples', 0) + samples.size
    if count > 16000 * 120 or not np.isfinite(samples).all():
        demo.SESSIONS.pop(session_id, None)
        return jsonify({'error': 'audio exceeds sample budget or contains nonfinite samples'}), 400
    session.recordian_samples = count
    demo.asr.streaming_transcribe(samples, session.state)
    return jsonify({'language': getattr(session.state, 'language', '') or '',
                    'text': getattr(session.state, 'text', '') or ''})


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
    parser.add_argument('--token-file', default='', help='Private Bearer token; required for LAN bind')
    parser.add_argument('--tls-cert-file', default='')
    parser.add_argument('--tls-key-file', default='')
    return parser


def main(argv: list[str] | None = None) -> None:
    global demo  # noqa: PLW0603

    args = build_parser().parse_args(argv)

    from recordian.local_auth import http_request_allowed, load_private_token, require_private_bind, server_tls_context
    token = load_private_token(args.token_file) if args.token_file else ''
    tls = server_tls_context(args.tls_cert_file, args.tls_key_file)
    require_private_bind(args.host, token, encrypted=tls is not None)

    import threading

    import qwen_asr.cli.demo_streaming as _demo
    from flask import g, jsonify, request
    from qwen_asr import Qwen3ASRModel

    demo = _demo
    demo.app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    request_slot = threading.BoundedSemaphore(1)
    @demo.app.before_request
    def authenticate():
        import hmac
        from urllib.parse import urlsplit
        if not http_request_allowed(host=urlsplit(request.host_url).hostname or '', origin=request.headers.get('Origin'),
                                    base_url=request.host_url, authenticated=bool(token)):
            return jsonify({'error': 'forbidden origin or host'}), 403
        if token and not hmac.compare_digest(request.headers.get('Authorization', '').encode('utf-8'),
                                             ('Bearer ' + token).encode('utf-8')):
            return jsonify({'error': 'unauthorized'}), 401
        if request.method == 'POST':
            if not request_slot.acquire(blocking=False):
                return jsonify({'error': 'model is busy'}), 429
            g.recordian_model_slot = True

    @demo.app.teardown_request
    def release_slot(_error):
        if getattr(g, 'recordian_model_slot', False):
            g.recordian_model_slot = False
            request_slot.release()
    model = args.model

    @demo.app.get("/v1/models")
    def list_models():
        return jsonify({"object": "list", "data": [{"id": model, "object": "model"}]})

    @demo.app.post("/v1/audio/transcriptions")
    def transcribe_upload():
        import tempfile
        import wave

        from recordian.audio_budget import decode_pcm16

        upload = request.files.get("file")
        if upload is None:
            return jsonify({"error": "file required"}), 400
        language = _force_language(request.form.get("language") or "")
        prompt = _resolve_context(request.form.get("prompt"))
        try:
            pcm = decode_pcm16(upload.read())
            with tempfile.NamedTemporaryFile(suffix='.wav') as handle:
                with wave.open(handle.name, 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(pcm)
                results = demo.asr.transcribe(audio=handle.name, context=prompt, language=language,
                                              return_time_stamps=False)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        result = results[0]
        return jsonify({
            "text": result.text or "",
            "language": getattr(result, "language", "") or "",
            "model": model,
        })

    demo.app.view_functions["api_start"] = _api_start
    demo.app.view_functions['api_chunk'] = _api_chunk

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
    from recordian.http_service import run_http_service
    run_http_service(demo.app, host=args.host, port=args.port, tls=tls)


if __name__ == "__main__":
    main()
