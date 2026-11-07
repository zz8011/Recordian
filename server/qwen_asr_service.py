"""Qwen3-ASR-1.7B production service -> the eval harness's B-arm path, served.

The wave-2 A/B verdict (2026-11-08) measured ``QwenASRProvider`` loaded
in-process by ``tools/asr_ab_eval.py`` with one exact parameter set. This
service hosts THAT provider class with THOSE parameters behind a small
loopback HTTP API, so the production numbers stay the same numbers the
eval produced:

  model            models/Qwen3-ASR-1.7B (local weights, --model)
  device / dtype   cuda:0 / bfloat16
  language         None (auto-detect; fixed, not per-request)
  max_new_tokens   1024

``server/asr_server.py`` is deliberately NOT reused: it bypasses
QwenASRProvider (no Silero VAD trim, its own hotword context string),
so its output would not be the B arm's output.

API (JSON, loopback only, Bearer auth from an owner-only file):
  GET  /health     -> {"status": "ok", "model": ..., "provider": ...}
  POST /transcribe   {"audio_base64": "<wav>", "hotwords": [...],
                      "context": "..."}
                   -> {"text": ..., "detected_language": ...,
                       "model": ..., "decode_elapsed_s": ...}

One request slot: the model decodes serially (single user, one GPU);
a concurrent request gets 429 and the client retries within its own
deadline instead of queueing unboundedly behind a long decode.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import logging
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from recordian.audio_budget import validate_wav
from recordian.http_service import run_http_service
from recordian.local_auth import http_request_allowed, load_private_token, require_private_bind

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# One PTT press is one request. 300 s covers every observed production
# dictation (wave-2 max was 98 s) with headroom; beyond it a request is
# rejected with a clear error instead of an unbounded decode. The
# pipeline client splits longer sessions into <=150 s requests.
MAX_AUDIO_SECONDS = 300.0

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 48 * 1024 * 1024

# Bearer credential for the local API (empty = loopback-only, no header
# required). Kept on a namespace so tests can swap it without reload.
_AUTH = SimpleNamespace(bearer="")
_provider = None
_provider_lock = threading.Lock()
_request_slot = threading.BoundedSemaphore(1)


def init_provider(provider: object) -> None:
    """Install the ASR provider (the real one in main, a fake in tests)."""
    global _provider
    _provider = provider


@app.before_request
def authenticate():
    from urllib.parse import urlsplit

    if not http_request_allowed(
        host=urlsplit(request.host_url).hostname or "",
        origin=request.headers.get("Origin"),
        base_url=request.host_url,
        authenticated=bool(_AUTH.bearer),
    ):
        return jsonify({"error": "forbidden origin or host"}), 403
    if _AUTH.bearer and not hmac.compare_digest(
        request.headers.get("Authorization", "").encode("utf-8"),
        ("Bearer " + _AUTH.bearer).encode("utf-8"),
    ):
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.route("/health", methods=["GET"])
def health_check():
    if _provider is None:
        return jsonify({"status": "error", "message": "model not loaded"}), 503
    return jsonify(
        {
            "status": "ok",
            "model": str(getattr(_provider, "model_name", "")),
            "provider": "qwen-asr-service",
        }
    )


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if _provider is None:
        return jsonify({"error": "model not loaded"}), 503
    if not _request_slot.acquire(blocking=False):
        return jsonify({"error": "model busy"}), 429
    try:
        return _transcribe_request()
    finally:
        _request_slot.release()


def _transcribe_request():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data:
            return jsonify({"error": "invalid JSON body"}), 400

        audio_b64 = data.get("audio_base64")
        if not isinstance(audio_b64, str) or not audio_b64:
            return jsonify({"error": "missing audio_base64"}), 400
        hotwords = data.get("hotwords", [])
        if not isinstance(hotwords, list) or not all(isinstance(h, str) for h in hotwords):
            return jsonify({"error": "hotwords must be a list of strings"}), 400
        context = data.get("context", "")
        if not isinstance(context, str):
            return jsonify({"error": "context must be a string"}), 400

        try:
            audio_data = base64.b64decode(audio_b64, validate=True)
            if not audio_data:
                return jsonify({"error": "empty audio"}), 400
            validate_wav(audio_data, max_seconds=MAX_AUDIO_SECONDS)
        except Exception as exc:  # noqa: BLE001 - a 400 category, never the detail
            return jsonify({"error": f"invalid wav: {type(exc).__name__}"}), 400

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(audio_data)
            temp_path = Path(handle.name)
        try:
            # The provider instance carries the context (as in the eval
            # harness, where it was fixed at construction). Single-slot
            # serialisation above makes swapping it per request exact.
            with _provider_lock:
                _provider.context = context
                started = time.perf_counter()
                result = _provider.transcribe_file(temp_path, hotwords=list(hotwords))
                decode_elapsed = time.perf_counter() - started
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

        text = str(getattr(result, "text", "") or "")
        logger.info(
            "transcribed %d audio bytes -> %d chars in %.2fs",
            len(audio_data),
            len(text),
            decode_elapsed,
        )
        return jsonify(
            {
                "text": text,
                "detected_language": getattr(result, "detected_language", None),
                "model": str(getattr(result, "model_name", "") or getattr(_provider, "model_name", "")),
                "decode_elapsed_s": round(decode_elapsed, 3),
            }
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals to the client
        logger.exception("transcription failed")
        return jsonify({"error": "transcription_failed"}), 500


def build_provider(model: str, device: str, dtype: str, max_new_tokens: int):
    """Construct the B-arm provider with the harness's exact parameters."""
    from recordian.providers.qwen_asr import QwenASRProvider

    provider = QwenASRProvider(
        model_name=model,
        device=device,
        dtype=dtype,
        language=None,
        max_new_tokens=max_new_tokens,
        context="",
    )
    provider._lazy_load()  # fail at startup, not on the first dictation
    return provider


def main() -> None:
    parser = argparse.ArgumentParser(description="Recordian Qwen3-ASR production service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8323)
    parser.add_argument("--model", required=True, help="Local Qwen3-ASR weights directory")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--token-file", default="", help="Owner-only Bearer credential file (0600)")
    args = parser.parse_args()

    if args.token_file:
        _AUTH.bearer = load_private_token(args.token_file)
    require_private_bind(args.host, _AUTH.bearer)

    logger.info("loading Qwen3-ASR model: %s", args.model)
    init_provider(build_provider(args.model, args.device, args.dtype, args.max_new_tokens))
    logger.info("model ready; serving on %s:%d", args.host, args.port)
    run_http_service(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
