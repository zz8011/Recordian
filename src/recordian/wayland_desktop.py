"""Hyprland desktop integration for native Wayland editors and overlays."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from .exceptions import CommitError
from .linux_commit import CommitResult, CompositionRefusedError, FcitxCommitter, TextCommitter


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

    def begin_composition(self, initial_preview: str = "") -> WaylandBufferedSession:
        session = WaylandBufferedSession(self)
        result = session.update_preedit(initial_preview)
        if not result.committed:
            raise CompositionRefusedError(result.detail)
        return session

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


class WaylandBufferedSession:
    """Let ASR rotate while buffering text for one final, focus-checked paste.

    Preview acceptance never writes to the application or clipboard. The
    continuous worker owns the bounded transcript; this session only guards
    delivery to the original window and permanently latches invalidation.
    """

    backend_name = "wayland-clipboard"
    supports_segments = False
    supports_buffered_continuous = True

    def __init__(self, committer: WaylandClipboardCommitter):
        self.committer = committer
        self.active = True
        self.stale_reason = ""

    def update_preedit(self, text: str) -> CommitResult:
        if not self.active:
            return CommitResult(self.backend_name, False, "session_closed", "cancelled")
        try:
            self.committer._check_focus()
        except (CommitError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.active = False
            self.stale_reason = str(exc)
            return CommitResult(self.backend_name, False, "window_unavailable", "stale")
        return CommitResult(self.backend_name, True, "preview_buffered", "buffered")

    def commit_segment(self, text: str) -> CommitResult:
        result = self.update_preedit(text)
        if not result.committed:
            return result
        return CommitResult(self.backend_name, False, "final_paste_only", "buffered")

    def commit(self, text: str) -> CommitResult:
        result = self.update_preedit(text)
        if not result.committed:
            return result
        # Consume before invoking external tools: a lost reply must never
        # permit a second paste, even if the first one reached the editor.
        self.active = False
        try:
            return self.committer.commit(text)
        except CommitError:
            return CommitResult(self.backend_name, False, "window_changed", "stale")
        except (OSError, subprocess.SubprocessError):
            return CommitResult(self.backend_name, False, "paste_failed", "uncertain")

    def cancel(self) -> CommitResult:
        self.active = False
        return CommitResult(self.backend_name, False, "cancelled", "cancelled")


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
    address = active.get("address")
    committer.target_window_address = address if isinstance(address, str) else ""
    committer.target_xwayland = bool(active.get("xwayland", True))
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
