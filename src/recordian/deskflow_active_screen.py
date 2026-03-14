from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DESKFLOW_ACTIVE_SCREEN_PATH = "~/.local/state/deskflow/active_screen.json"
DEFAULT_DESKFLOW_LOG_PATH = ""
_LOG_SWITCH_RE = re.compile(r'switch from "(?P<from>[^"]+)" to "(?P<to>[^"]+)"')
_LOG_READ_SIZE_BYTES = 512 * 1024


@dataclass(slots=True)
class DeskflowActiveScreenState:
    screen: str
    updated_at: str
    server_name: str
    path: str
    source: str = "state-file"


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
        source="state-file",
    )


def resolve_deskflow_active_screen(
    *,
    state_path_value: str,
    log_path_value: str = "",
) -> DeskflowActiveScreenState:
    try:
        return load_deskflow_active_screen(state_path_value)
    except Exception as state_exc:  # noqa: BLE001
        try:
            return load_deskflow_active_screen_from_log(log_path_value)
        except Exception as log_exc:  # noqa: BLE001
            raise RuntimeError(f"state_unavailable:{state_exc};log_unavailable:{log_exc}") from log_exc


def load_deskflow_active_screen_from_log(path_value: str) -> DeskflowActiveScreenState:
    candidates = list(iter_deskflow_log_candidates(path_value))
    if not candidates:
        raise FileNotFoundError("deskflow_log_path_not_configured")

    for path in candidates:
        if not path.is_file():
            continue
        state = _parse_active_screen_from_log(path)
        if state is not None:
            return state
    raise FileNotFoundError("deskflow_log_switch_record_not_found")


def iter_deskflow_log_candidates(path_value: str) -> Iterable[Path]:
    raw_path = str(path_value).strip()
    seen: set[str] = set()

    def _yield(path: Path) -> Iterable[Path]:
        resolved = str(path.expanduser())
        if not resolved or resolved in seen:
            return []
        seen.add(resolved)
        return [Path(resolved)]

    if raw_path:
        yield from _yield(Path(raw_path))
        return

    home = Path.home()
    xdg_config_home = Path.home() / ".config"
    xdg_state_home = Path.home() / ".local" / "state"

    for candidate in (
        xdg_config_home / "Deskflow" / "deskflow-daemon.log",
        xdg_config_home / "deskflow" / "deskflow-daemon.log",
        xdg_config_home / "Deskflow" / "deskflow.log",
        xdg_config_home / "deskflow" / "deskflow.log",
        xdg_state_home / "Deskflow" / "deskflow-daemon.log",
        xdg_state_home / "deskflow" / "deskflow-daemon.log",
        home / "deskflow.log",
    ):
        yield from _yield(candidate)


def _parse_active_screen_from_log(path: Path) -> DeskflowActiveScreenState | None:
    text = _read_log_tail(path, max_bytes=_LOG_READ_SIZE_BYTES)
    latest_match = None
    for line in text.splitlines():
        match = _LOG_SWITCH_RE.search(line)
        if match is not None:
            latest_match = match
    if latest_match is None:
        return None

    timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="milliseconds")
    return DeskflowActiveScreenState(
        screen=latest_match.group("to").strip(),
        updated_at=timestamp.replace("+00:00", "Z"),
        server_name="",
        path=str(path),
        source="log-file",
    )


def _read_log_tail(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max(1, int(max_bytes))))
        data = handle.read()
    return data.decode("utf-8", errors="replace")
