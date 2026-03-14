from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DESKFLOW_ACTIVE_SCREEN_PATH = "~/.local/state/deskflow/active_screen.json"


@dataclass(slots=True)
class DeskflowActiveScreenState:
    screen: str
    updated_at: str
    server_name: str
    path: str


def load_deskflow_active_screen(path_value: str) -> DeskflowActiveScreenState:
    raw_path = str(path_value).strip() or DEFAULT_DESKFLOW_ACTIVE_SCREEN_PATH
    path = Path(raw_path).expanduser()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("deskflow_active_screen_payload_not_object")

    screen = str(payload.get("screen", "")).strip()
    if not screen:
        raise ValueError("deskflow_active_screen_missing_screen")

    return DeskflowActiveScreenState(
        screen=screen,
        updated_at=str(payload.get("updated_at", "")).strip(),
        server_name=str(payload.get("server_name", "")).strip(),
        path=str(path),
    )
