from __future__ import annotations

import argparse
import logging
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any

from recordian.linux_commit import _set_clipboard_text

from .protocol import (
    DEFAULT_REMOTE_PASTE_PORT,
    DEFAULT_REMOTE_PASTE_TIMEOUT_S,
    MAX_MESSAGE_BYTES,
    decode_message,
    encode_message,
    preview_text,
)

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_PASTE_HOST = "192.168.5.111"
DEFAULT_REMOTE_PASTE_MODE = "direct"
DEFAULT_REMOTE_PASTE_SYNC_WAIT_S = 0.35


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
    parser.add_argument(
        "--remote-paste-mode",
        choices=["direct", "shared-clipboard"],
        default=DEFAULT_REMOTE_PASTE_MODE,
        help="Remote paste transport: direct sends text over TCP; shared-clipboard stages local clipboard for DeskFlow/Synergy-style sync",
    )
    parser.add_argument(
        "--remote-paste-sync-wait-s",
        type=float,
        default=DEFAULT_REMOTE_PASTE_SYNC_WAIT_S,
        help="When using shared-clipboard mode, wait this many seconds after staging the local clipboard before asking the remote agent to paste",
    )


def send_remote_paste(host: str, text: str, *, port: int, timeout_s: float) -> RemotePasteResult:
    request = {
        "action": "paste",
        "text": text,
        "timestamp": int(time.time()),
    }
    return _send_remote_command(host, request, port=port, timeout_s=timeout_s)


def send_remote_paste_via_shared_clipboard(
    host: str,
    text: str,
    *,
    port: int,
    timeout_s: float,
    sync_wait_s: float,
) -> RemotePasteResult:
    _set_clipboard_text(text)
    request = {
        "action": "paste_only",
        "source": "shared-clipboard",
        "preview": preview_text(text, max_len=48),
        "expected_text": text,
        "clipboard_wait_s": max(0.0, float(sync_wait_s)),
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
    mode = str(getattr(args, "remote_paste_mode", DEFAULT_REMOTE_PASTE_MODE)).strip() or DEFAULT_REMOTE_PASTE_MODE
    sync_wait_s = float(getattr(args, "remote_paste_sync_wait_s", DEFAULT_REMOTE_PASTE_SYNC_WAIT_S))

    result: dict[str, Any] = {
        "enabled": enabled,
        "attempted": False,
        "sent": False,
        "host": host,
        "port": port,
        "mode": mode,
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
        if mode == "shared-clipboard":
            response = send_remote_paste_via_shared_clipboard(
                host,
                text,
                port=port,
                timeout_s=timeout_s,
                sync_wait_s=sync_wait_s,
            )
        else:
            response = send_remote_paste(host, text, port=port, timeout_s=timeout_s)
        result.update(
            {
                "sent": response.ok,
                "status": response.status,
                "detail": response.detail,
                "response": response.response,
            }
        )
        mode_label = "共享剪贴板" if mode == "shared-clipboard" else "直接传输"
        message = (
            f"[RemotePaste] 以{mode_label}模式向 {host}:{port} 发送粘贴命令成功"
            if response.ok
            else f"[RemotePaste] 以{mode_label}模式向 {host}:{port} 发送粘贴命令失败: {response.detail}"
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
