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
    python asr_server.py --host 0.0.0.0 --port 8000 --model Qwen/Qwen3-ASR-1.7B
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 全局变量：ASR 模型
asr_model = None
model_name = None


def load_asr_model(model_path: str, device: str = "cuda:0") -> None:
    """加载 ASR 模型到内存"""
    global asr_model, model_name

    logger.info(f"Loading ASR model: {model_path}")

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
        data = request.json
        if not data:
            return jsonify({"error": "Invalid JSON"}), 400

        audio_base64 = data.get("audio_base64")
        if not audio_base64:
            return jsonify({"error": "Missing audio_base64"}), 400

        hotwords = data.get("hotwords", [])

        # 解码音频
        try:
            audio_data = base64.b64decode(audio_base64)
        except Exception as e:
            return jsonify({"error": f"Invalid base64: {e}"}), 400

        # 保存到临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        try:
            # 识别
            logger.info(f"Transcribing audio: {len(audio_data)} bytes")
            results = asr_model.transcribe(
                audio=temp_path,
                context="",
                language=None,
                return_time_stamps=False,
            )

            result = results[0]
            text = (result.text or "").strip()

            logger.info(f"Transcription result: {text[:50]}...")

            return jsonify({
                "text": text,
                "confidence": 0.95,  # Qwen3-ASR 不提供置信度
                "model": model_name,
            })

        finally:
            # 清理临时文件
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Transcription error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def main():
    parser = argparse.ArgumentParser(description="Recordian ASR HTTP Server")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind (default: 8000)",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen3-ASR-1.7B",
        help="ASR model path (default: Qwen/Qwen3-ASR-1.7B)",
    )
    parser.add_argument(
        "--device",
        default="cuda:0",
        help="Device to use (default: cuda:0)",
    )

    args = parser.parse_args()

    # 加载模型
    load_asr_model(args.model, args.device)

    # 启动服务器
    logger.info(f"Starting ASR server on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
