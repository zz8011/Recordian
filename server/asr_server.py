#!/usr/bin/env python3
"""Recordian ASR HTTP Server

提供 Qwen3-ASR 语音识别 HTTP API 服务，供局域网内的 Recordian 客户端调用。

API 端点：
    POST /transcribe
        请求体：{"audio_base64": "...", "hotwords": [...]}
        响应：{"text": "...", "confidence": 0.95, "model": "qwen3-asr-1.7b"}

    GET /health
        健康检查端点
        响应：{"status": "ok", "model": "qwen3-asr-1.7b"}

使用方法：
    python asr_server.py --host 127.0.0.1 --port 8000 --model Qwen/Qwen3-ASR-0.6B
"""

from __future__ import annotations

import argparse
import base64
import hmac
import logging
import os
import tempfile
import threading

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from recordian.audio_budget import validate_wav
from recordian.http_service import run_http_service
from recordian.local_auth import http_request_allowed, load_private_token, require_private_bind, server_tls_context

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
auth_token = ''
_model_lock = threading.Lock()
_request_slot = threading.BoundedSemaphore(1)


@app.before_request
def authenticate():
    from urllib.parse import urlsplit
    if not http_request_allowed(host=urlsplit(request.host_url).hostname or '', origin=request.headers.get('Origin'),
                                base_url=request.host_url, authenticated=bool(auth_token)):
        return jsonify({'error': 'forbidden origin or host'}), 403
    if auth_token and not hmac.compare_digest(
            request.headers.get('Authorization', '').encode('utf-8'),
            ('Bearer ' + auth_token).encode('utf-8')):
        return jsonify({'error': 'unauthorized'}), 401

# 全局变量：ASR 模型
asr_model = None
model_name = None


def _normalize_hotwords(hotwords: object) -> list[str]:
    if not isinstance(hotwords, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in hotwords:
        token = str(raw).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _compose_context(*, context: object, hotwords: object) -> str:
    base_context = str(context or "").strip()
    normalized_hotwords = _normalize_hotwords(hotwords)
    if not normalized_hotwords:
        return base_context

    hotword_hint = "热词: " + ", ".join(normalized_hotwords)
    if not base_context:
        return hotword_hint
    return f"{base_context}\n{hotword_hint}"


def load_asr_model(model_path: str, device: str = "cuda:0", *, max_new_tokens: int = 8192) -> None:
    """加载 ASR 模型到内存"""
    global asr_model, model_name

    logger.info(f"Loading ASR model: {model_path} (max_new_tokens={max_new_tokens})")

    try:
        import torch
        from qwen_asr import Qwen3ASRModel
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "qwen-asr not installed. Run: pip install qwen-asr"
        ) from exc

    asr_model = Qwen3ASRModel.from_pretrained(
        model_path,
        dtype=torch.bfloat16,
        device_map=device,
        max_new_tokens=max_new_tokens,
    )
    model_name = model_path
    logger.info(f"ASR model loaded: {model_path}")


@app.route("/health", methods=["GET"])
def health_check():
    """健康检查端点"""
    if asr_model is None:
        return jsonify({"status": "error", "message": "Model not loaded"}), 503

    return jsonify({
        "status": "ok",
        "model": model_name,
        "device": str(asr_model.device) if hasattr(asr_model, "device") else "unknown",
    })


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if not _request_slot.acquire(blocking=False):
        return jsonify({'error': 'model is busy'}), 429
    try:
        return _transcribe_request()
    finally:
        _request_slot.release()


def _transcribe_request():
    """语音识别端点

    请求体：
        {
            "audio_base64": "base64 编码的 WAV 音频",
            "hotwords": ["可选的热词列表"]
        }

    响应：
        {
            "text": "识别结果",
            "confidence": 0.95,
            "model": "qwen3-asr-1.7b"
        }
    """
    if asr_model is None:
        return jsonify({"error": "Model not loaded"}), 503

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data:
            return jsonify({"error": "Invalid JSON"}), 400

        audio_base64 = data.get("audio_base64")
        if not isinstance(audio_base64, str) or not audio_base64:
            return jsonify({"error": "Missing audio_base64"}), 400

        hotwords = _normalize_hotwords(data.get("hotwords", []))
        context = _compose_context(context=data.get("context", ""), hotwords=hotwords)
        if len(context) > 4000:
            return jsonify({'error': 'context exceeds 4000 characters'}), 400
        raw_language = data.get("language")
        language = str(raw_language).strip() or None if raw_language is not None else None

        # 可选：客户端可单请求指定最大生成 token 数
        requested_max_new_tokens = data.get("max_new_tokens")
        if requested_max_new_tokens is not None:
            try:
                requested_max_new_tokens = int(requested_max_new_tokens)
                if not 1 <= requested_max_new_tokens <= 8192:
                    return jsonify({'error': 'max_new_tokens exceeds the server budget'}), 400
            except (TypeError, ValueError, OverflowError):
                return jsonify({'error': 'invalid max_new_tokens'}), 400

        # 解码音频
        try:
            audio_data = base64.b64decode(audio_base64, validate=True)
            if not audio_data:
                return jsonify({'error': 'empty audio'}), 400
            validate_wav(audio_data)
        except Exception as e:
            return jsonify({"error": f"Invalid base64: {e}"}), 400

        # 保存到临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        # 临时应用单请求的 max_new_tokens（不污染全局模型配置）
        try:
            # Serialize the model and its per-request generation setting.
            with _model_lock:
                original_max_new_tokens = getattr(asr_model, 'max_new_tokens', None)
                if requested_max_new_tokens is not None and requested_max_new_tokens > int(original_max_new_tokens or 8192):
                    return jsonify({'error': 'max_new_tokens exceeds the server budget'}), 400
                try:
                    if requested_max_new_tokens is not None and original_max_new_tokens is not None:
                        asr_model.max_new_tokens = requested_max_new_tokens
                    logger.info('Transcribing %s audio bytes, context_chars=%s', len(audio_data), len(context))
                    results = asr_model.transcribe(audio=temp_path, context=context, language=language,
                                                   return_time_stamps=False)
                    used_max_new_tokens = getattr(asr_model, 'max_new_tokens', None)
                finally:
                    if original_max_new_tokens is not None:
                        asr_model.max_new_tokens = original_max_new_tokens

            result = results[0]
            text = (result.text or "").strip()

            logger.info('Transcription completed, text_chars=%s', len(text))

            return jsonify({
                "text": text,
                "confidence": 0.95,  # Qwen3-ASR 不提供置信度
                "model": model_name,
                "applied_hotwords": hotwords,
                "applied_context": context,
                "requested_language": language,
                "max_new_tokens": used_max_new_tokens,
            })
        finally:
            # 清理临时文件
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Transcription error: {e}", exc_info=True)
        return jsonify({"error": "transcription_failed"}), 500


def main():
    global auth_token
    parser = argparse.ArgumentParser(description="Recordian ASR HTTP Server")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1); LAN binds require --token-file",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind (default: 8000)",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen3-ASR-0.6B",
        help="ASR model path (default: Qwen/Qwen3-ASR-0.6B)",
    )
    parser.add_argument(
        "--device",
        default="cuda:0",
        help="Device to use (default: cuda:0)",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=8192,
        help="Maximum ASR generation tokens per request (default: 8192)",
    )

    parser.add_argument('--token-file', default='', help='Owner-only Bearer token file (0600)')
    parser.add_argument('--tls-cert-file', default='')
    parser.add_argument('--tls-key-file', default='')
    args = parser.parse_args()
    auth_token = load_private_token(args.token_file) if args.token_file else ''
    tls = server_tls_context(args.tls_cert_file, args.tls_key_file)
    require_private_bind(args.host, auth_token, encrypted=tls is not None)

    # 加载模型
    load_asr_model(args.model, args.device, max_new_tokens=args.max_new_tokens)

    # 启动服务器
    logger.info(f"Starting ASR server on {args.host}:{args.port}")
    run_http_service(app, host=args.host, port=args.port, tls=tls)


if __name__ == "__main__":
    main()
