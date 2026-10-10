"""Bounded, read-only discovery for preferences. Never runs an Agent CLI."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentCandidate:
    kind: str
    name: str
    executable: str
    supported: bool


AGENTS = (
    ("hermes", "Hermes"), ("codex", "Codex"), ("claude", "Claude Code"),
    ("gemini", "Gemini CLI"), ("opencode", "OpenCode"), ("aider", "Aider"),
)


def discover_agents(path=None, home=None):
    """Look at executable metadata only: no --version, login or shim execution."""
    search_path = os.environ.get("PATH", "") if path is None else path
    home = Path.home() if home is None else Path(home)
    extra = [home / ".local/bin", home / ".cargo/bin", home / "bin"]
    search_path = os.pathsep.join([search_path, *(str(p) for p in extra)])
    result = []
    for kind, name in AGENTS:
        executable = shutil.which(kind, path=search_path)
        result.append(AgentCandidate(kind, name, executable or "", kind in {"hermes", "claude"}))
    return result


def _query(argv):
    """Only fixed, harmless enumeration commands are used by callers."""
    output = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=True).stdout
    if len(output) > 1024 * 1024:
        raise ValueError("device inventory too large")
    return output


def microphone_choices(backend, current="default"):
    choices = [("default", "跟随系统默认麦克风")]
    try:
        use_pulse = backend != "arecord" and shutil.which("ffmpeg") and shutil.which("pactl")
        if use_pulse:
            sources = json.loads(_query(["pactl", "--format=json", "list", "sources"]))
            for source in sources[:128]:
                name = source.get("name", "")
                if name and not name.endswith(".monitor"):
                    choices.append((name, str(source.get("description") or name)[:90]))
        elif backend == "arecord" or (backend == "auto" and not shutil.which("ffmpeg")):
            name = None
            for line in _query(["arecord", "-L"]).splitlines()[:1024]:
                if line and not line[0].isspace():
                    name = line.strip()
                elif name and line.strip():
                    if name != "default" and name != "null":
                        choices.append((name, line.strip()[:90]))
                    name = None
    except (OSError, ValueError, subprocess.SubprocessError, TypeError, AttributeError):
        pass
    unique = dict(choices)
    if current and current not in unique:
        unique[current] = "已配置 · 本次未检测到"
    return list(unique.items())


def language_choices(provider, current="auto"):
    # These two forced-language paths are documented/tested by the packaged
    # Qwen/Confucius provider. HTTP services have no language inventory contract.
    choices = [("auto", "自动识别")]
    if provider in {"confucius-asr", "qwen-asr"}:
        choices += [("Chinese", "中文"), ("English", "英语")]
    if current and current not in dict(choices):
        choices.append((current, "已配置 · " + current))
    return choices


def read_autostart():
    """Read only this unit's enablement; never starts or changes a unit."""
    try:
        state = _query(["systemctl", "--user", "show", "recordian-desktop.service", "--property=UnitFileState", "--value"]).strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return True if state == "enabled" else False if state == "disabled" else None
