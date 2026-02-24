#!/usr/bin/env python3
"""测试 ASR 服务器

使用本地音频文件测试 ASR HTTP 服务器是否正常工作。

使用方法：
    python test_asr_server.py --server http://192.168.5.225:8000 --audio test.wav
"""

import argparse
import base64
import json
from pathlib import Path
from urllib import request


def test_health(server_url: str) -> None:
    """测试健康检查端点"""
    print(f"测试健康检查: {server_url}/health")

    try:
        req = request.Request(f"{server_url}/health", method="GET")
        with request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"✅ 服务正常")
            print(f"   模型: {data.get('model')}")
            print(f"   设备: {data.get('device')}")
            print(f"   状态: {data.get('status')}")
    except Exception as e:
        print(f"❌ 健康检查失败: {e}")
        raise


def test_transcribe(server_url: str, audio_path: Path) -> None:
    """测试语音识别端点"""
    print(f"\n测试语音识别: {audio_path}")

    if not audio_path.exists():
        raise FileNotFoundError(f"音频文件不存在: {audio_path}")

    # 读取音频文件
    audio_data = audio_path.read_bytes()
    print(f"音频大小: {len(audio_data)} bytes")

    # 编码为 base64
    audio_base64 = base64.b64encode(audio_data).decode("ascii")

    # 构建请求
    payload = {
        "audio_base64": audio_base64,
        "hotwords": [],
    }

    headers = {
        "Content-Type": "application/json",
    }

    req = request.Request(
        f"{server_url}/transcribe",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    print("发送请求...")
    try:
        with request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"✅ 识别成功")
            print(f"   文本: {result.get('text')}")
            print(f"   置信度: {result.get('confidence')}")
            print(f"   模型: {result.get('model')}")
    except Exception as e:
        print(f"❌ 识别失败: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(description="测试 ASR 服务器")
    parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="ASR 服务器地址 (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        help="测试音频文件路径 (WAV 格式)",
    )

    args = parser.parse_args()

    print("=== Recordian ASR 服务器测试 ===\n")

    # 测试健康检查
    test_health(args.server)

    # 测试语音识别
    if args.audio:
        test_transcribe(args.server, args.audio)
    else:
        print("\n⚠️  未指定音频文件，跳过识别测试")
        print("使用 --audio 参数指定测试音频文件")

    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    main()
