from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import ssl
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import numpy as np

from .audio import chunk_samples, read_wav_mono_f32

_SAMPLE_RATE = 16000
_OPENAI_BETA_HEADER = ("OpenAI-Beta", "realtime=v1")


class WebSocketHandshakeError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        reason: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"WebSocket handshake failed: HTTP {status_code} {reason}".strip())
        self.status_code = status_code
        self.reason = reason
        self.headers = headers or {}


@dataclass
class ProbeConfig:
    wav_path: Path
    model: str
    url: str = "http://127.0.0.1:8000"
    chunk_ms: int = 320
    start_after_chunks: int = 1
    realtime_speed: float = 1.0
    connect_timeout_s: float = 5.0
    final_timeout_s: float = 10.0
    api_key: str | None = None
    send_openai_beta_header: bool = True
    verbose: bool = False


@dataclass
class ProbeResult:
    url: str
    chunk_count: int
    connected: bool = False
    session_created: bool = False
    started_generation: bool = False
    done: bool = False
    full_text: str = ""
    event_types: list[str] = field(default_factory=list)
    error: str | None = None


def normalize_realtime_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse(f"http://{url.strip()}")

    scheme_map = {
        "http": "ws",
        "https": "wss",
        "ws": "ws",
        "wss": "wss",
    }
    scheme = scheme_map.get(parsed.scheme.lower())
    if not scheme:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme}")

    path = parsed.path or ""
    if not path or path == "/":
        path = "/v1/realtime"
    elif not path.rstrip("/").endswith("/v1/realtime"):
        path = path.rstrip("/") + "/v1/realtime"

    return urlunparse(
        (
            scheme,
            parsed.netloc,
            path,
            "",
            parsed.query,
            "",
        )
    )


def _compute_websocket_accept(key: str) -> str:
    payload = (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
    digest = hashlib.sha1(payload).digest()
    return base64.b64encode(digest).decode("ascii")


def _float_to_pcm16le(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2", copy=False)
    return pcm.tobytes()


def _build_append_event(samples: np.ndarray) -> dict[str, str]:
    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(_float_to_pcm16le(samples)).decode("ascii"),
    }


def _build_session_update(model: str) -> dict[str, str]:
    return {"type": "session.update", "model": model}


def _build_commit_event(*, final: bool) -> dict[str, object]:
    return {"type": "input_audio_buffer.commit", "final": final}


def _build_client_frame(opcode: int, payload: bytes) -> bytes:
    mask = secrets.token_bytes(4)
    payload_len = len(payload)

    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))

    if payload_len < 126:
        header.append(0x80 | payload_len)
    elif payload_len < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", payload_len))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", payload_len))

    masked_payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return bytes(header) + mask + masked_payload


def _decode_close_payload(payload: bytes) -> tuple[int | None, str]:
    if len(payload) < 2:
        return None, ""
    code = struct.unpack(">H", payload[:2])[0]
    reason = payload[2:].decode("utf-8", errors="replace")
    return code, reason


def _describe_handshake_failure(exc: WebSocketHandshakeError) -> str:
    if exc.status_code == 403:
        return (
            "服务端拒绝了 WebSocket 握手（HTTP 403）。"
            "这通常表示当前 vLLM 实例未启用 /v1/realtime、模型不支持 realtime，或需要额外鉴权。"
        )
    return str(exc)


class _SimpleWebSocketClient:
    def __init__(
        self,
        url: str,
        *,
        connect_timeout_s: float,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.url = normalize_realtime_url(url)
        self.connect_timeout_s = connect_timeout_s
        self.headers = headers or []
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"unsupported websocket scheme: {parsed.scheme}")

        host = parsed.hostname
        if not host:
            raise ValueError(f"invalid websocket URL: {self.url}")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        ssl_context = ssl.create_default_context() if parsed.scheme == "wss" else None
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context),
            timeout=self.connect_timeout_s,
        )

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        resource = parsed.path or "/"
        if parsed.query:
            resource += f"?{parsed.query}"

        request_lines = [
            f"GET {resource} HTTP/1.1",
            f"Host: {parsed.netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for header_name, header_value in self.headers:
            request_lines.append(f"{header_name}: {header_value}")
        request = ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii")

        self.writer.write(request)
        await self.writer.drain()

        response = await asyncio.wait_for(
            self.reader.readuntil(b"\r\n\r\n"),
            timeout=self.connect_timeout_s,
        )
        status_line, *raw_headers = response.decode("iso-8859-1").split("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2:
            raise RuntimeError(f"invalid websocket handshake response: {status_line!r}")
        status_code = int(parts[1])
        reason = parts[2] if len(parts) > 2 else ""
        headers: dict[str, str] = {}
        for line in raw_headers:
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        if status_code != 101:
            await self.close()
            raise WebSocketHandshakeError(status_code, reason, headers)

        expected_accept = _compute_websocket_accept(key)
        if headers.get("sec-websocket-accept") != expected_accept:
            await self.close()
            raise RuntimeError("invalid Sec-WebSocket-Accept in handshake response")

    async def send_json(self, payload: dict[str, object]) -> None:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await self._send_frame(0x1, text.encode("utf-8"))

    async def receive_json(self) -> dict[str, object]:
        while True:
            opcode, payload = await self._read_frame()
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 0x8:
                code, reason = _decode_close_payload(payload)
                raise ConnectionError(f"websocket closed by server ({code}): {reason}")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue

    async def close(self) -> None:
        writer = self.writer
        self.writer = None
        self.reader = None
        if writer is None:
            return
        try:
            writer.write(_build_client_frame(0x8, b""))
            await writer.drain()
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.writer is None:
            raise RuntimeError("websocket is not connected")
        self.writer.write(_build_client_frame(opcode, payload))
        await self.writer.drain()

    async def _read_frame(self) -> tuple[int, bytes]:
        if self.reader is None:
            raise RuntimeError("websocket is not connected")

        header = await self.reader.readexactly(2)
        first_byte, second_byte = header
        opcode = first_byte & 0x0F
        payload_len = second_byte & 0x7F
        masked = bool(second_byte & 0x80)

        if payload_len == 126:
            payload_len = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack(">Q", await self.reader.readexactly(8))[0]

        mask = await self.reader.readexactly(4) if masked else b""
        payload = await self.reader.readexactly(payload_len)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload


async def run_probe(config: ProbeConfig) -> ProbeResult:
    wav_path = config.wav_path.expanduser().resolve()
    samples = read_wav_mono_f32(wav_path, sample_rate=_SAMPLE_RATE)
    chunks = chunk_samples(samples, sample_rate=_SAMPLE_RATE, chunk_ms=config.chunk_ms)
    if not chunks:
        raise ValueError(f"音频为空: {wav_path}")

    headers: list[tuple[str, str]] = []
    if config.api_key:
        headers.append(("Authorization", f"Bearer {config.api_key}"))
    if config.send_openai_beta_header:
        headers.append(_OPENAI_BETA_HEADER)

    client = _SimpleWebSocketClient(
        config.url,
        connect_timeout_s=config.connect_timeout_s,
        headers=headers,
    )
    result = ProbeResult(url=client.url, chunk_count=len(chunks))
    done_event = asyncio.Event()

    async def receiver() -> None:
        try:
            while True:
                event = await client.receive_json()
                event_type = str(event.get("type", ""))
                if event_type:
                    result.event_types.append(event_type)

                if event_type == "session.created":
                    result.session_created = True
                    continue

                if event_type == "transcription.delta":
                    delta = str(event.get("delta", ""))
                    result.full_text += delta
                    if delta:
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                    continue

                if event_type == "transcription.done":
                    result.done = True
                    final_text = str(event.get("text", "")).strip()
                    if final_text:
                        result.full_text = final_text
                    done_event.set()
                    continue

                if event_type == "error":
                    message = str(event.get("error", "unknown error"))
                    code = str(event.get("code", "")).strip()
                    result.error = f"{code}: {message}" if code else message
                    done_event.set()
                    continue

                if config.verbose:
                    print(f"[probe] event={json.dumps(event, ensure_ascii=False)}", file=sys.stderr)
        except ConnectionError as exc:
            if not result.done and result.error is None:
                result.error = str(exc)
                done_event.set()

    await client.connect()
    result.connected = True

    receiver_task = asyncio.create_task(receiver())

    try:
        await client.send_json(_build_session_update(config.model))
        start_threshold = max(1, min(config.start_after_chunks, len(chunks)))

        for index, chunk in enumerate(chunks, start=1):
            await client.send_json(_build_append_event(chunk))
            if not result.started_generation and index >= start_threshold:
                await client.send_json(_build_commit_event(final=False))
                result.started_generation = True

            if index < len(chunks):
                chunk_duration_s = len(chunk) / _SAMPLE_RATE
                await asyncio.sleep(chunk_duration_s / max(config.realtime_speed, 0.01))

        if not result.started_generation:
            await client.send_json(_build_commit_event(final=False))
            result.started_generation = True

        await client.send_json(_build_commit_event(final=True))
        await asyncio.wait_for(done_event.wait(), timeout=config.final_timeout_s)
    finally:
        await client.close()
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass

    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vLLM /v1/realtime WAV 探针")
    parser.add_argument("--wav", required=True, help="16kHz PCM16 WAV 文件路径")
    parser.add_argument("--model", default="Qwen3-ASR-0.6B", help="服务端模型名")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="vLLM 服务地址或完整 /v1/realtime URL")
    parser.add_argument("--chunk-ms", type=int, default=320, help="发送分块时长（毫秒）")
    parser.add_argument("--start-after-chunks", type=int, default=1, help="累计多少块后发送首次 commit")
    parser.add_argument("--realtime-speed", type=float, default=1.0, help="发送速度倍数；1.0 为真实时间")
    parser.add_argument("--connect-timeout", type=float, default=5.0, help="连接超时（秒）")
    parser.add_argument("--final-timeout", type=float, default=10.0, help="发送完成后等待 done 的超时（秒）")
    parser.add_argument("--api-key", default="", help="可选 Bearer token")
    parser.add_argument("--no-openai-beta-header", action="store_true", help="不发送 OpenAI-Beta: realtime=v1")
    parser.add_argument("--verbose", action="store_true", help="打印额外事件")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    config = ProbeConfig(
        wav_path=Path(args.wav),
        model=args.model,
        url=args.url,
        chunk_ms=args.chunk_ms,
        start_after_chunks=args.start_after_chunks,
        realtime_speed=args.realtime_speed,
        connect_timeout_s=args.connect_timeout,
        final_timeout_s=args.final_timeout,
        api_key=args.api_key.strip() or None,
        send_openai_beta_header=not args.no_openai_beta_header,
        verbose=args.verbose,
    )

    try:
        result = asyncio.run(run_probe(config))
    except WebSocketHandshakeError as exc:
        print(f"[probe] {normalize_realtime_url(config.url)}", file=sys.stderr)
        print(f"[probe] {_describe_handshake_failure(exc)}", file=sys.stderr)
        return 2
    except TimeoutError:
        print("[probe] 等待 realtime 响应超时", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"[probe] 失败: {exc}", file=sys.stderr)
        return 1

    if result.full_text:
        print(file=sys.stdout)

    print(f"[probe] url={result.url}", file=sys.stderr)
    print(f"[probe] chunks={result.chunk_count} done={result.done}", file=sys.stderr)
    if result.error:
        print(f"[probe] server_error={result.error}", file=sys.stderr)
        return 4
    if not result.done:
        print("[probe] 未收到 transcription.done", file=sys.stderr)
        return 5
    print(f"[probe] final_text={result.full_text}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
