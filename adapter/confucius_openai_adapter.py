#!/usr/bin/env python3
"""Confucius ASR (WebSocket) -> OpenAI-compatible /v1/audio/transcriptions adapter.

Orca 桌面听写的 "OpenAI-compatible" provider 只会发 multipart POST；
本机 Confucius 服务只讲 WebSocket 流式协议（server/confucius_streaming_server.py）。
本适配器把两者接起来：multipart 音频 -> ffmpeg 归一为 16k mono s16le -> 按 160ms 帧推给 Confucius
-> 收集增量文本 -> {"text": ...}。

只绑 127.0.0.1；token 从文件读（不硬编码）；仅依赖标准库 + websockets + 系统 ffmpeg。
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

HOST = "127.0.0.1"
DEFAULT_PORT = 8391
WS_URL = os.environ.get("CONFUCIUS_WS_URL", "ws://127.0.0.1:8321/asr_stream_api_v1")
TOKEN_FILE = Path(
    os.environ.get("CONFUCIUS_TOKEN_FILE")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "recordian" / "confucius_server_token.txt"
)
MODEL_ID = "confucius4-r2t2"
SAMPLE_RATE = 16000
FRAME_BYTES = SAMPLE_RATE * 160 // 1000 * 2  # 160ms PCM16 mono，与 Recordian 客户端一致
EOS = "YOUDAO_ONETIME_ASR_STREAM_EOS"
SEGMENT_S = 28  # Confucius 单会话预算 30s，留 2s 余量
MAX_AUDIO_S = 600  # 与 Orca 单次听写上限一致
MAX_BODY = 64 * 1024 * 1024
BUSY_RETRIES = 5  # 服务单并发，忙时 CLOSE 4429，间隔 1s 重试
LANG_MAP = {"zh": "Chinese", "zh-cn": "Chinese", "zh-hans": "Chinese", "en": "English", "en-us": "English"}

_LOCK = threading.Lock()  # 上游单并发：适配器自身请求串行


class AdapterError(Exception):
    def __init__(self, status: int, message: str, kind: str = "adapter_error"):
        super().__init__(message)
        self.status, self.kind = status, kind


def to_pcm(data: bytes) -> bytes:
    """任意容器(wav/webm/ogg/mp3) -> 16k mono s16le。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-protocol_whitelist", "pipe", "-i", "pipe:0",
             "-t", str(MAX_AUDIO_S + 1), "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
            input=data, capture_output=True, timeout=120,
        )  # fmt: skip
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(500, f"ffmpeg unavailable or timed out: {type(exc).__name__}") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise AdapterError(
            400, f"ffmpeg decode failed: {proc.stderr.decode(errors='replace')[:200]}", "invalid_request"
        )
    if len(proc.stdout) > MAX_AUDIO_S * SAMPLE_RATE * 2:
        raise AdapterError(400, f"audio exceeds {MAX_AUDIO_S}s", "invalid_request")
    return proc.stdout


def _segment(pcm: bytes, language: str, token: str) -> str:
    """一个 Confucius 会话：header -> connected -> 帧 -> EOS -> 收增量直到 reset。"""
    header = {"requestId": uuid.uuid4().hex, "secret_key": token, "language": language,
              "sample_rate": SAMPLE_RATE, "channels": 1}  # fmt: skip
    parts: list[str] = []
    with connect(WS_URL, max_size=2**22, open_timeout=10) as ws:
        ws.send(json.dumps(header))
        greeting = json.loads(ws.recv(timeout=30))
        if greeting.get("status") != "connected":
            raise AdapterError(502, str(greeting.get("msg") or "connection rejected"))
        for i in range(0, len(pcm), FRAME_BYTES):
            ws.send(pcm[i : i + FRAME_BYTES])
        ws.send(EOS)
        while True:
            msg = json.loads(ws.recv(timeout=60))
            if msg.get("status") == "error":
                raise AdapterError(502, str(msg.get("msg") or "transcription error"))
            body = msg.get("msg") or {}
            if msg.get("status") == "success":
                if body.get("text"):
                    parts.append(body["text"])
                if body.get("reset"):
                    return "".join(parts)


def _segment_retry(pcm: bytes, language: str, token: str) -> str:
    for attempt in range(BUSY_RETRIES):
        try:
            return _segment(pcm, language, token)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            if code == 4401:
                raise AdapterError(502, "Confucius rejected the token (4401)") from exc
            if code != 4429:
                raise AdapterError(502, f"Confucius closed the connection (code={code})") from exc
            if attempt + 1 < BUSY_RETRIES:
                time.sleep(1)
        except (OSError, TimeoutError) as exc:
            raise AdapterError(502, f"Confucius unreachable: {type(exc).__name__}: {exc}") from exc
    raise AdapterError(503, "Confucius is busy (4429), retry later", "server_busy")


def _join(parts: list[str]) -> str:
    out = ""
    for p in (s.strip() for s in parts):
        if out and p and out[-1].isascii() and out[-1].isalnum() and p[0].isascii() and p[0].isalnum():
            out += " "  # 英文分段拼接补空格；中文直接相连
        out += p
    return out


def transcribe(audio: bytes, language: str | None) -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AdapterError(500, f"cannot read token file: {exc.strerror}") from exc
    lang = (language or "").strip()
    # 实测：服务端 auto(zhen) 对有损音频(ogg)会丢尾字，显式 Chinese 完整，故空/auto 默认 Chinese
    lang = "Chinese" if lang.lower() in {"", "auto", "detect"} else LANG_MAP.get(lang.lower(), lang)
    pcm = to_pcm(audio)
    # shortcut: 按固定 28s 硬切，可能切在词中间，需要时改成按静音点切
    step = SEGMENT_S * SAMPLE_RATE * 2
    with _LOCK:
        return _join([_segment_retry(pcm[i : i + step], lang, token) for i in range(0, len(pcm), step)])


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str, bytes]]:
    raw = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + body
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    if not msg.is_multipart():
        raise AdapterError(400, "expected multipart/form-data", "invalid_request")
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[str(name)] = (part.get_filename() or "", bytes(part.get_payload(decode=True) or b""))
    return fields


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ConfuciusAdapter/1"

    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _forbidden(self) -> bool:
        """防 DNS rebinding / 浏览器跨站：Host 与 Origin 必须是回环。"""
        loopback = {"127.0.0.1", "localhost", "::1"}
        host = urlsplit("//" + self.headers.get("Host", "")).hostname
        origin = self.headers.get("Origin")
        if host not in loopback or (origin and urlsplit(origin).hostname not in loopback):
            self._send(403, {"error": {"message": "forbidden host or origin", "type": "forbidden"}})
            return True
        return False

    def _read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks, total = [], 0
            while size := int(self.rfile.readline().split(b";")[0].strip() or b"0", 16):
                total += size
                if total > MAX_BODY:
                    raise AdapterError(413, "body too large", "invalid_request")
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise AdapterError(413, "body too large", "invalid_request")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if self._forbidden():
            return
        if urlsplit(self.path).path.rstrip("/") == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
        else:
            self._send(404, {"error": {"message": "not found", "type": "not_found"}})

    def do_POST(self) -> None:
        if self._forbidden():
            return
        try:
            if urlsplit(self.path).path.rstrip("/") != "/v1/audio/transcriptions":
                raise AdapterError(404, "not found", "not_found")
            fields = parse_multipart(self.headers.get("Content-Type", ""), self._read_body())
            if "file" not in fields or not fields["file"][1]:
                raise AdapterError(400, "missing file field", "invalid_request")
            language = fields.get("language", ("", b""))[1].decode(errors="replace")
            self._send(200, {"text": transcribe(fields["file"][1], language)})
        except AdapterError as exc:
            self._send(exc.status, {"error": {"message": str(exc), "type": exc.kind}})
        except Exception as exc:  # noqa: BLE001 - 以 OpenAI 错误格式返回，不让连接直接断
            self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}", "type": "internal_error"}})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("CONFUCIUS_ADAPTER_PORT", DEFAULT_PORT)))
    port = ap.parse_args().port
    try:
        server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as exc:
        sys.exit(f"cannot bind {HOST}:{port}: {exc}（端口冲突请用 --port 或 CONFUCIUS_ADAPTER_PORT 换端口）")
    print(f"confucius adapter listening on http://{HOST}:{port}/v1 -> {WS_URL}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
