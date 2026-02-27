from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from shutil import which
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)


class CommitError(RuntimeError):
    pass


@dataclass(slots=True)
class CommitResult:
    backend: str
    committed: bool
    detail: str = ""


class TextCommitter:
    backend_name = "base"

    def commit(self, text: str) -> CommitResult:
        raise NotImplementedError


class NoopCommitter(TextCommitter):
    backend_name = "none"

    def commit(self, text: str) -> CommitResult:
        return CommitResult(backend=self.backend_name, committed=False, detail="disabled")


class StdoutCommitter(TextCommitter):
    backend_name = "stdout"

    def commit(self, text: str) -> CommitResult:
        print(text, file=sys.stderr)
        return CommitResult(backend=self.backend_name, committed=False, detail="printed_to_stderr")


class WTypeCommitter(TextCommitter):
    backend_name = "wtype"

    def commit(self, text: str) -> CommitResult:
        if not which("wtype"):
            raise CommitError("wtype not found in PATH")
        _run_command(["wtype", "--", text])
        return CommitResult(backend=self.backend_name, committed=True)


class XDoToolCommitter(TextCommitter):
    """xdotool type — fallback for apps that don't handle clipboard paste well."""
    backend_name = "xdotool"

    def commit(self, text: str) -> CommitResult:
        if not which("xdotool"):
            raise CommitError("xdotool not found in PATH")
        _run_command(["xdotool", "type", "--delay", "1", "--clearmodifiers", "--", text])
        return CommitResult(backend=self.backend_name, committed=True)


def _parse_clipboard_timeout_ms(env_value: str | None) -> int:
    """Parse and validate clipboard timeout from environment variable.

    Returns 0 (disabled) if invalid or out of range.
    Valid range: 0-60000ms (0-60 seconds).
    """
    if not env_value:
        return 0
    try:
        timeout_ms = int(env_value)
        if timeout_ms < 0 or timeout_ms > 60000:
            return 0
        return timeout_ms
    except ValueError:
        return 0


class XdotoolClipboardCommitter(TextCommitter):
    """Write to clipboard via xclip, then simulate paste with xdotool.

    More reliable than xdotool type for CJK text and Electron apps (Obsidian etc.).
    """
    backend_name = "xdotool-clipboard"

    def __init__(self, target_window_id: int | None = None, clipboard_timeout_ms: int = 0) -> None:
        self.target_window_id = target_window_id
        self.clipboard_timeout_ms = clipboard_timeout_ms
        self._clear_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    def commit(self, text: str) -> CommitResult:
        if not which("xdotool"):
            raise CommitError("xdotool not found in PATH")
        _set_clipboard_text(text)

        # 取消之前的定时器（如果存在）
        with self._timer_lock:
            if self._clear_timer is not None:
                self._clear_timer.cancel()
                self._clear_timer = None

            # 启动新的定时器清空剪贴板
            if self.clipboard_timeout_ms > 0:
                def _clear_clipboard():
                    try:
                        _set_clipboard_text("")
                    except Exception:
                        pass  # 静默失败，不影响主流程

                self._clear_timer = threading.Timer(
                    self.clipboard_timeout_ms / 1000.0,
                    _clear_clipboard
                )
                self._clear_timer.daemon = True
                self._clear_timer.start()

        time.sleep(0.10)
        shortcut = _resolve_paste_shortcut()
        # Terminals use Ctrl+Shift+V; override if target is a terminal.
        wid = self.target_window_id
        if shortcut == "ctrl+v" and wid is not None and _is_terminal_window(wid):
            shortcut = "ctrl+shift+v"
        _xdotool_key(shortcut, window_id=wid)
        detail = f"paste:{shortcut}" + (f" wid:{wid}" if wid else "")
        if self.clipboard_timeout_ms > 0:
            detail += f" clear_after:{self.clipboard_timeout_ms}ms"
        return CommitResult(backend=self.backend_name, committed=True, detail=detail)


def resolve_committer(backend: str, *, target_window_id: int | None = None) -> TextCommitter:
    """Resolve text output backend for Linux desktop integration."""
    normalized = backend.strip().lower()
    if normalized == "none":
        return NoopCommitter()
    if normalized == "stdout":
        return StdoutCommitter()
    if normalized == "wtype":
        return WTypeCommitter()
    if normalized == "xdotool":
        return XDoToolCommitter()
    if normalized == "xdotool-clipboard":
        timeout_ms = _parse_clipboard_timeout_ms(os.environ.get("RECORDIAN_CLIPBOARD_TIMEOUT_MS"))
        return XdotoolClipboardCommitter(
            target_window_id=target_window_id,
            clipboard_timeout_ms=timeout_ms
        )
    if normalized == "auto":
        # Prefer xdotool-clipboard: handles CJK and Electron apps correctly on X11.
        if which("xdotool") and (which("xclip") or which("xsel")):
            timeout_ms = _parse_clipboard_timeout_ms(os.environ.get("RECORDIAN_CLIPBOARD_TIMEOUT_MS"))
            return XdotoolClipboardCommitter(
                target_window_id=target_window_id,
                clipboard_timeout_ms=timeout_ms
            )
        if which("wtype"):
            return WTypeCommitter()
        if which("xdotool"):
            return XDoToolCommitter()
        raise CommitError(
            "No text commit backend available. Please install xdotool+xclip or wtype:\n"
            "  sudo apt install xdotool xclip  # for X11\n"
            "  sudo apt install wtype          # for Wayland"
        )
    raise ValueError(f"unsupported commit backend: {backend}")


def get_focused_window_id() -> int | None:
    """Return the currently focused X11 window ID via xdotool, or None on failure."""
    if not which("xdotool"):
        return None
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return None


# Known terminal emulator WM_CLASS names (lowercase).
_TERMINAL_WM_CLASSES = {
    "gnome-terminal-server", "gnome-terminal",
    "konsole",
    "xterm",
    "uxterm",
    "rxvt",
    "urxvt",
    "alacritty",
    "kitty",
    "tilix",
    "terminator",
    "xfce4-terminal",
    "lxterminal",
    "mate-terminal",
    "st",
    "foot",
    "wezterm",
    "hyper",
}


def _is_terminal_window(window_id: int) -> bool:
    """Return True if the given X11 window belongs to a terminal emulator."""
    if not which("xprop"):
        return False
    try:
        result = subprocess.run(
            ["xprop", "-id", str(window_id), "WM_CLASS"],
            capture_output=True,
            text=True,
            check=False,
        )
        # WM_CLASS(STRING) = "gnome-terminal-server", "Gnome-terminal"
        line = result.stdout.lower()
        return any(name in line for name in _TERMINAL_WM_CLASSES)
    except (FileNotFoundError, OSError):
        return False


def _run_command(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise CommitError(f"command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise CommitError(f"command failed: {exc}") from exc


def _is_ibus_running() -> bool:
    """Check if ibus-daemon is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "ibus-daemon"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _resolve_paste_shortcut() -> str:
    """Resolve the paste shortcut from environment variable or use default.

    Returns the paste shortcut (e.g., "ctrl+v", "shift+insert").
    Can be overridden via RECORDIAN_PASTE_SHORTCUT environment variable.
    """
    env_shortcut = os.environ.get("RECORDIAN_PASTE_SHORTCUT")
    if env_shortcut:
        return env_shortcut.strip().lower()
    # Default to ctrl+v
    return "ctrl+v"


def _set_clipboard_text(text: str) -> None:
    if which("wl-copy"):
        _run_command_with_input(["wl-copy", "--type", "text/plain;charset=utf-8"], text)
        return
    if which("xsel"):
        # xsel holds clipboard ownership after exit; more reliable than xclip.
        _run_command_with_input(["xsel", "--clipboard", "--input"], text)
        return
    if which("xclip"):
        # xclip drops clipboard when process exits; use xdotool to paste immediately after.
        _run_command_with_input(["xclip", "-selection", "clipboard", "-i"], text)
        return

    # Fallback without external deps.
    try:
        import tkinter as tk
    except ModuleNotFoundError as exc:
        raise CommitError("clipboard backend unavailable: need wl-copy/xclip/xsel or tkinter") from exc
    root = tk.Tk()
    root.withdraw()
    root.clipboard_clear()
    if text:
        root.clipboard_append(text)
    root.update()
    root.destroy()


def _xdotool_key(shortcut: str, *, window_id: int | None = None) -> None:
    """Send a key combination via xdotool."""
    token = shortcut.replace(" ", "").replace("_", "+")
    xdotool_key = token.replace("insert", "Insert")
    if window_id is not None:
        # Focus the target window first; --window alone is ignored by many apps.
        try:
            subprocess.run(
                ["xdotool", "windowfocus", "--sync", str(window_id)],
                check=True,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        time.sleep(0.15)  # Electron apps need more time to transfer focus to input field
    cmd = ["xdotool", "key", "--clearmodifiers", xdotool_key]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise CommitError("xdotool not found") from exc
    except subprocess.CalledProcessError as exc:
        raise CommitError(f"xdotool key failed: {exc}") from exc


def _run_command_with_input(cmd: list[str], text: str) -> None:
    try:
        proc = subprocess.run(cmd, input=text, text=True, capture_output=True, check=True)
    except FileNotFoundError as exc:
        raise CommitError(f"command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise CommitError(f"command failed: {cmd} {detail}") from exc
