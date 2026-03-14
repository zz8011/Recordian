from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import DEFAULT_REMOTE_PASTE_PORT


@dataclass(slots=True)
class RemotePasteAgentConfig:
    port: int = DEFAULT_REMOTE_PASTE_PORT
    hostname: str = socket.gethostname()
    enable_notify: bool = True
    notify_backend: str = "auto"
    paste_delay_ms: int = 100
    log_level: str = "INFO"
    log_file: str = ""
    commit_backend: str = "auto"


def load_agent_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError("config_root_must_be_object")
        return payload

    values: dict[str, Any] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"invalid_config_line:{lineno}")
        key, value = line.split(":", 1)
        values[key.strip()] = _parse_scalar(value.strip())
    return values


def _parse_scalar(raw: str) -> Any:
    if not raw:
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]

    lowered = raw.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw
