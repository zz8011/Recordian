from __future__ import annotations

import argparse
import logging
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any

from .protocol import (
    DEFAULT_REMOTE_PASTE_PORT,
    DEFAULT_REMOTE_PASTE_TIMEOUT_S,
    MAX_MESSAGE_BYTES,
    decode_message,
    encode_message,
)

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_PASTE_HOST = "192.168.5.111"


@dataclass(slots=True)
class RemotePasteResult:
    host: str
    port: int
    ok: bool
    status: str
    detail: str
    response: dict[str, Any]


def add_remote_paste_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--enable-remote-paste",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Send final text to a remote Recordian paste agent after local processing",
    )
    parser.add_argument(
        "--remote-paste-host",
        default=DEFAULT_REMOTE_PASTE_HOST,
        help="Remote Recordian agent host for single-target paste",
    )
    parser.add_argument(
        "--remote-paste-port",
        type=int,
        default=DEFAULT_REMOTE_PASTE_PORT,
        help=f"Remote Recordian agent TCP port (default: {DEFAULT_REMOTE_PASTE_PORT})",
    )
    parser.add_argument(
        "--remote-paste-timeout-s",
        type=float,
        default=DEFAULT_REMOTE_PASTE_TIMEOUT_S,
        help="Timeout for remote paste TCP request in seconds",
    )


def send_remote_paste(host: str, text: str, *, port: int, timeout_s: float) -> RemotePasteResult:
    request = {
        "action": "paste",
        "text": text,
        "timestamp": int(time.time()),
    }
    return _send_remote_command(host, request, port=port, timeout_s=timeout_s)


def _send_remote_command(host: str, payload: dict[str, Any], *, port: int, timeout_s: float) -> RemotePasteResult:
    timeout = max(0.1, float(timeout_s))
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encode_message(payload))
        raw = _read_response_line(sock)
    response = decode_message(raw)
    status = str(response.get("status", "")).strip() or "error"
    detail = str(response.get("detail", "")).strip() or status
    return RemotePasteResult(
        host=host,
        port=port,
        ok=status == "ok",
        status=status,
        detail=detail,
        response=response,
    )


def _read_response_line(sock: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_MESSAGE_BYTES:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.extend(chunk)
        if b"\n" in chunk:
            line, _sep, _rest = bytes(chunks).partition(b"\n")
            return line
    if len(chunks) > MAX_MESSAGE_BYTES:
        raise RuntimeError("response_too_large")
    if not chunks:
        raise RuntimeError("empty_response")
    return bytes(chunks)


def send_remote_paste_from_args(
    args: argparse.Namespace,
    text: str,
    *,
    log: Any | None = None,
) -> dict[str, Any]:
    enabled = bool(getattr(args, "enable_remote_paste", False))
    host = str(getattr(args, "remote_paste_host", "")).strip()
    port = int(getattr(args, "remote_paste_port", DEFAULT_REMOTE_PASTE_PORT))
    timeout_s = float(getattr(args, "remote_paste_timeout_s", DEFAULT_REMOTE_PASTE_TIMEOUT_S))

    result: dict[str, Any] = {
        "enabled": enabled,
        "attempted": False,
        "sent": False,
        "host": host,
        "port": port,
        "detail": "disabled",
    }
    if not enabled:
        return result
    if not text.strip():
        result["detail"] = "empty_text"
        return result
    if not host:
        result["detail"] = "host_not_configured"
        return result

    result["attempted"] = True
    try:
        response = send_remote_paste(host, text, port=port, timeout_s=timeout_s)
        result.update(
            {
                "sent": response.ok,
                "status": response.status,
                "detail": response.detail,
                "response": response.response,
            }
        )
        message = (
            f"[RemotePaste] 向 {host}:{port} 发送粘贴命令成功"
            if response.ok
            else f"[RemotePaste] 向 {host}:{port} 发送粘贴命令失败: {response.detail}"
        )
        _log_message(log, message)
    except Exception as exc:  # noqa: BLE001
        result.update({"sent": False, "status": "error", "detail": str(exc)})
        _log_message(log, f"[RemotePaste] 向 {host}:{port} 发送粘贴命令失败: {exc}")
    return result


def _log_message(log: Any | None, message: str) -> None:
    if callable(log):
        log(message)
        return
    logger.info(message)


def result_as_dict(result: RemotePasteResult) -> dict[str, Any]:
    return asdict(result)
