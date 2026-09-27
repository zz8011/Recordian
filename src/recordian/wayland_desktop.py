"""Hyprland desktop integration for native Wayland editors and overlays."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from .exceptions import CommitError
from .linux_commit import CommitResult, FcitxCommitter, TextCommitter


def desktop_query(subject: str):
    result = subprocess.run(
        ["hyprctl", subject, "-j"], capture_output=True, text=True, timeout=0.6, check=True,
    )
    return json.loads(result.stdout)


class WaylandClipboardCommitter(TextCommitter):
    """One final paste into the same native window that started dictation.

    Does not open a stale Fcitx context or inject changing ASR hypotheses.
    A focus change refuses delivery; we never reactivate an old window.
    """

    backend_name = "wayland-clipboard"

    def __init__(self, address: str):
        self.address = address

    def _check_focus(self):
        if not self.address or desktop_query("activewindow").get("address") != self.address:
            raise CommitError("输入窗口已经改变，未粘贴识别结果")

    def commit(self, text: str) -> CommitResult:
        self._check_focus()
        subprocess.run(
            ["wl-copy", "--type", "text/plain;charset=utf-8"], input=text,
            text=True, check=True, timeout=2,
        )
        time.sleep(0.12)
        self._check_focus()
        subprocess.run(
            ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], check=True, timeout=2,
        )
        return CommitResult(backend=self.backend_name, committed=True, detail="paste:ctrl+v")


def select_desktop_committer(committer: TextCommitter) -> TextCommitter:
    # Older Codex launches without Wayland IME support need final-paste
    # compatibility. Once the running process enables IME, retain Fcitx
    # composition so ASR hypotheses and corrected words update inline.
    if not isinstance(committer, FcitxCommitter) or not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return committer
    try:
        active = desktop_query("activewindow")
    except (OSError, ValueError, subprocess.SubprocessError):
        return committer
    if active.get("class", "").lower() in {"chatgpt", "codex"} and not active.get("xwayland", True):
        if _wayland_ime_enabled(active.get("pid")):
            return committer
        return WaylandClipboardCommitter(active.get("address", ""))
    return committer


def _wayland_ime_enabled(pid: object) -> bool:
    """Inspect the actual launch, not flags saved for the next restart."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    arguments = [argument for argument in arguments if argument]
    # Electron can rewrite argv into one space-separated process title.
    if len(arguments) == 1:
        try:
            return "--enable-wayland-ime" in shlex.split(arguments[0].decode())
        except (UnicodeError, ValueError):
            return False
    return b"--enable-wayland-ime" in arguments


def place_overlay_on_active_monitor() -> bool:
    """Position an already mapped orb using compositor logical coordinates."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return False
    try:
        monitors = desktop_query("monitors")
        active = desktop_query("activewindow")
        monitor = next((m for m in monitors if m["id"] == active.get("monitor")), None)
        if monitor is None:
            monitor = next(m for m in monitors if m.get("focused"))
        windows = desktop_query("clients")
        orb = next(w for w in windows if w.get("class") == "Recordian Overlay")
        width, height = orb["size"]
        scale = float(monitor["scale"])
        x = round(monitor["x"] + (monitor["width"] / scale - width) / 2)
        y = round(monitor["y"] + monitor["height"] / scale - height - 48)
        selector = json.dumps("address:" + orb["address"])
        monitor_id = json.dumps(str(monitor["id"]))
        lua = (
            f'hl.dispatch(hl.dsp.window.move({{monitor={monitor_id},follow=false,window={selector}}}));'
            f'hl.dispatch(hl.dsp.window.move({{x={x},y={y},relative=false,window={selector}}}));'
            f'return hl.dispatch(hl.dsp.window.alter_zorder({{mode="top",window={selector}}}))'
        )
        result = subprocess.run(["hyprctl", "eval", lua], capture_output=True, text=True, timeout=0.6)
        return result.returncode == 0 and "error:" not in result.stdout
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        return False
