from __future__ import annotations

import json
from typing import Any

DEFAULT_REMOTE_PASTE_PORT = 24872
DEFAULT_REMOTE_PASTE_TIMEOUT_S = 3.0
MAX_MESSAGE_BYTES = 1024 * 1024


class RemotePasteProtocolError(RuntimeError):
    pass


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def decode_message(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    stripped = text.strip()
    if not stripped:
        raise RemotePasteProtocolError("empty_message")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RemotePasteProtocolError(f"invalid_json:{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise RemotePasteProtocolError("message_must_be_object")
    return payload


def preview_text(text: str, *, max_len: int = 20) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."
