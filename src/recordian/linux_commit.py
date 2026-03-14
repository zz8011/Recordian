from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from shutil import which

from .exceptions import CommitError

logger = logging.getLogger(__name__)

_CLIPBOARD_SETTLE_DELAY_S = 0.22


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

    def __init__(self, target_window_id: int | None = None) -> None:
        self.target_window_id = target_window_id

    def commit(self, text: str) -> CommitResult:
        if not which("xdotool"):
            raise CommitError("xdotool not found in PATH")
        if isinstance(self.target_window_id, int):
            _xdotool_focus_window(self.target_window_id)
            time.sleep(0.12)
        _run_command(["xdotool", "type", "--delay", "1", "--clearmodifiers", "--", text])
        return CommitResult(backend=self.backend_name, committed=True)


def send_paste_shortcut(*, target_window_id: int | None = None) -> CommitResult:
    if not which("xdotool"):
        raise CommitError("xdotool not found in PATH")
    shortcut = _resolve_paste_shortcut()
    if shortcut == "ctrl+v" and target_window_id is not None and _is_terminal_window(target_window_id):
        shortcut = "ctrl+shift+v"
    _xdotool_key(shortcut, window_id=target_window_id)
    detail = f"paste_only:{shortcut}"
    if target_window_id is not None:
        detail += f" wid:{target_window_id}"
    return CommitResult(backend="paste-only", committed=True, detail=detail)


def send_hard_enter(committer: TextCommitter) -> CommitResult:
    """Send a real Enter key event (not text newline) via current commit backend."""
    backend = getattr(committer, "backend_name", "unknown")
    try:
        if backend in {"none", "stdout"}:
            return CommitResult(backend=backend, committed=False, detail="hard_enter_unsupported_backend")

        # Prefer real keyboard simulation first. Some apps ignore xdotool key events
        # but accept physical-like key press/release from pynput.
        if _send_hard_enter_via_pynput():
            return CommitResult(backend=backend, committed=True, detail="hard_enter_sent:pynput")

        if backend == "wtype":
            if not which("wtype"):
                raise CommitError("wtype not found in PATH")
            _run_command(["wtype", "-k", "Return"])
            return CommitResult(backend=backend, committed=True, detail="hard_enter_sent")

        if backend in {"xdotool", "xdotool-clipboard", "auto"}:
            if not which("xdotool"):
                raise CommitError("xdotool not found in PATH")
            wid = getattr(committer, "target_window_id", None)
            _xdotool_key("return", window_id=wid if isinstance(wid, int) else None)
            detail = "hard_enter_sent"
            if isinstance(wid, int):
                detail += f" wid:{wid}"
            return CommitResult(backend=backend, committed=True, detail=detail)

        return CommitResult(backend=backend, committed=False, detail="hard_enter_unsupported_backend")
    except Exception as exc:  # noqa: BLE001
        return CommitResult(backend=backend, committed=False, detail=f"hard_enter_failed:{exc}")


def _send_hard_enter_via_pynput() -> bool:
    try:
        from pynput.keyboard import Controller, Key

        kb = Controller()
        kb.press(Key.enter)
        kb.release(Key.enter)
        return True
    except Exception:
        return False


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

    def __init__(
        self,
        target_window_id: int | None = None,
        clipboard_timeout_ms: int = 0,
    ) -> None:
        self.target_window_id = target_window_id
        self.clipboard_timeout_ms = clipboard_timeout_ms
        self._clear_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    def commit(self, text: str) -> CommitResult:
        if not which("xdotool"):
            raise CommitError("xdotool not found in PATH")
        clipboard_owner = _start_transient_clipboard_owner(text)
        if clipboard_owner is None:
            _set_clipboard_text(text)
        # Give the clipboard owner time to publish the new selection before
        # sending the paste shortcut. This avoids stale clipboard pastes on X11.
        time.sleep(_CLIPBOARD_SETTLE_DELAY_S)

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
                    except (subprocess.SubprocessError, OSError):
                        pass  # 静默失败，不影响主流程

                self._clear_timer = threading.Timer(
                    self.clipboard_timeout_ms / 1000.0,
                    _clear_clipboard
                )
                self._clear_timer.daemon = True
                self._clear_timer.start()

        try:
            result = send_paste_shortcut(target_window_id=self.target_window_id)
            detail = str(result.detail).replace("paste_only:", "paste:", 1)
            if self.clipboard_timeout_ms > 0:
                detail += f" clear_after:{self.clipboard_timeout_ms}ms"
            return CommitResult(backend=self.backend_name, committed=True, detail=detail)
        finally:
            _stop_transient_clipboard_owner(clipboard_owner)


class CommitterWithFallback(TextCommitter):
    """Wrapper that tries multiple committers with automatic fallback on failure.

    Attempts committers in order until one succeeds or all fail.
    Useful for handling environments where certain tools may be unavailable.

    Args:
        committers: List of (committer, description) tuples to try in order
        notify_on_fallback: Whether to send desktop notification on fallback
        max_timeout_per_attempt: Maximum time to wait for each attempt (seconds)
    """

    backend_name = "fallback"

    def __init__(
        self,
        committers: list[tuple[TextCommitter, str]],
        notify_on_fallback: bool = True,
        max_timeout_per_attempt: float = 2.0,
    ) -> None:
        if not committers:
            raise ValueError("CommitterWithFallback requires at least one committer")
        self.committers = committers
        self.notify_on_fallback = notify_on_fallback
        self.max_timeout_per_attempt = max_timeout_per_attempt
        # Use first committer's name as primary backend
        self.backend_name = f"{committers[0][0].backend_name}-fallback"

    def commit(self, text: str) -> CommitResult:
        """Try each committer in order until one succeeds."""
        last_error = None
        attempts = []

        for i, (committer, description) in enumerate(self.committers):
            try:
                # Try to commit with timeout protection
                result = committer.commit(text)

                # If this is not the first committer, we fell back
                if i > 0:
                    logger.warning(
                        f"Fallback to {committer.backend_name} succeeded "
                        f"after {i} failed attempt(s): {description}"
                    )
                    if self.notify_on_fallback:
                        try:
                            from .linux_notify import resolve_notifier
                            notifier = resolve_notifier("auto")
                            notifier.notify(
                                title="Recordian 输入降级",
                                message=f"使用备用方式: {description}",
                                urgency="low",
                            )
                        except Exception as e:
                            logger.debug(f"Failed to send fallback notification: {e}")

                # Add fallback info to result detail
                if i > 0:
                    detail = f"fallback:{i+1}/{len(self.committers)} {result.detail}"
                    return CommitResult(
                        backend=self.backend_name,
                        committed=result.committed,
                        detail=detail,
                    )
                return result

            except Exception as e:
                last_error = e
                attempts.append(f"{committer.backend_name}:{type(e).__name__}")
                logger.debug(
                    f"Committer {committer.backend_name} failed ({description}): {e}"
                )
                continue

        # All committers failed
        error_msg = f"All {len(self.committers)} committers failed: {', '.join(attempts)}"
        logger.error(error_msg)

        if self.notify_on_fallback:
            try:
                from .linux_notify import resolve_notifier
                notifier = resolve_notifier("auto")
                notifier.notify(
                    title="Recordian 输入失败",
                    message="所有输入方式均失败，请检查系统配置",
                    urgency="critical",
                )
            except Exception as e:
                logger.debug(f"Failed to send error notification: {e}")

        raise CommitError(error_msg) from last_error


def resolve_committer(backend: str, *, target_window_id: int | None = None) -> TextCommitter:
    """Resolve text output backend for Linux desktop integration.

    Args:
        backend: Backend name (auto, auto-fallback, xdotool, xdotool-clipboard, wtype, stdout, none)
        target_window_id: Optional X11 window ID for window-specific routing

    Returns:
        Configured TextCommitter instance

    Note:
        In 'auto' mode with target_window_id:
        - Electron apps (WeChat, VS Code, etc.) → xdotool-clipboard (required)
        - Terminal windows → xdotool-clipboard (Ctrl+Shift+V support)
        - Other windows → xdotool-clipboard (preferred for CJK)

        In 'auto-fallback' mode:
        - Same as 'auto' but with automatic fallback on failure
        - Tries: xdotool-clipboard → xdotool → wtype → stdout
    """
    normalized = backend.strip().lower()
    if normalized == "none":
        return NoopCommitter()
    if normalized == "stdout":
        return StdoutCommitter()
    if normalized == "wtype":
        return WTypeCommitter()
    if normalized == "xdotool":
        return XDoToolCommitter(target_window_id=target_window_id)
    if normalized == "xdotool-clipboard":
        timeout_ms = _parse_clipboard_timeout_ms(os.environ.get("RECORDIAN_CLIPBOARD_TIMEOUT_MS"))
        return XdotoolClipboardCommitter(
            target_window_id=target_window_id,
            clipboard_timeout_ms=timeout_ms
        )
    if normalized in ("auto", "auto-fallback"):
        # Auto mode: intelligent backend selection based on window type
        is_electron = False
        is_terminal = False

        if target_window_id is not None:
            # Detect window type for smart routing
            is_electron = _is_electron_window(target_window_id)
            is_terminal = _is_terminal_window(target_window_id)

            # Log detection results for debugging
            if is_electron:
                logger.debug(f"Detected Electron app (wid={target_window_id}), using xdotool-clipboard")
            elif is_terminal:
                logger.debug(f"Detected terminal (wid={target_window_id}), using xdotool-clipboard")

        # Build committer list for fallback mode
        if normalized == "auto-fallback":
            committers = []
            timeout_ms = _parse_clipboard_timeout_ms(os.environ.get("RECORDIAN_CLIPBOARD_TIMEOUT_MS"))

            # Try xdotool-clipboard first (best for CJK and Electron)
            if which("xdotool") and (which("xclip") or which("xsel")):
                committers.append((
                    XdotoolClipboardCommitter(target_window_id=target_window_id, clipboard_timeout_ms=timeout_ms),
                    "xdotool-clipboard"
                ))

            # Fallback to xdotool type
            if which("xdotool"):
                committers.append((
                    XDoToolCommitter(target_window_id=target_window_id),
                    "xdotool"
                ))

            # Fallback to wtype (Wayland)
            if which("wtype"):
                committers.append((
                    WTypeCommitter(),
                    "wtype"
                ))

            # Last resort: stdout
            committers.append((
                StdoutCommitter(),
                "stdout"
            ))

            if len(committers) > 1:
                return CommitterWithFallback(committers=committers, notify_on_fallback=True)
            elif committers:
                return committers[0][0]
            else:
                raise CommitError("No text commit backend available")

        # Regular auto mode (no fallback)
        # Prefer xdotool-clipboard: handles CJK and Electron apps correctly on X11.
        # Required for Electron apps due to complex input controls.
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

# Known Electron app WM_CLASS names (lowercase).
# These apps often have complex input controls that require special handling.
_ELECTRON_WM_CLASSES = {
    # WeChat variants
    "wechatappex", "wechat", "wechat.exe", "deepin-wine-wechat",
    # VS Code
    "code", "vscode", "code - insiders",
    # Obsidian
    "obsidian",
    # Slack
    "slack",
    # Discord
    "discord",
    # Other common Electron apps
    "atom",
    "github desktop",
    "postman",
    "notion",
    "figma",
}

# Cache for window detection results: {window_id: (is_electron, timestamp)}
# TTL: 5 seconds, Max size: 100 entries
_WINDOW_DETECTION_CACHE: dict[int, tuple[bool, float]] = {}
_CACHE_TTL = 5.0  # seconds
_CACHE_MAX_SIZE = 100


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
            timeout=1.0,
        )
        # WM_CLASS(STRING) = "gnome-terminal-server", "Gnome-terminal"
        # Use word boundaries to avoid false matches (e.g., "st" in "wechatappex")
        line = result.stdout.lower()
        # Extract class names from format: WM_CLASS(STRING) = "class1", "class2"
        if '"' in line:
            # Parse quoted strings
            import re
            classes = re.findall(r'"([^"]+)"', line)
            return any(cls in _TERMINAL_WM_CLASSES for cls in classes)
        return False
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _is_electron_window(window_id: int) -> bool:
    """Return True if the given X11 window belongs to an Electron app.

    Electron apps (WeChat, VS Code, Obsidian, etc.) often use custom input
    controls that require special handling for focus restoration.

    Results are cached for 5 seconds to avoid repeated xprop calls.

    Args:
        window_id: X11 window ID to check

    Returns:
        True if the window is an Electron app, False otherwise

    Note:
        - Returns False on Wayland sessions (xprop unavailable)
        - Returns False if xprop is not installed
        - Returns False on timeout or error (fail-safe)
        - Uses LRU cache with 5s TTL and 100 entry limit
    """
    # Check cache first
    current_time = time.time()
    if window_id in _WINDOW_DETECTION_CACHE:
        is_electron, timestamp = _WINDOW_DETECTION_CACHE[window_id]
        if current_time - timestamp < _CACHE_TTL:
            return is_electron
        # Cache expired, remove it
        del _WINDOW_DETECTION_CACHE[window_id]

    # Check if running on Wayland (xprop doesn't work)
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        result = False
        _WINDOW_DETECTION_CACHE[window_id] = (result, current_time)
        return result

    if not which("xprop"):
        result = False
        _WINDOW_DETECTION_CACHE[window_id] = (result, current_time)
        return result

    try:
        proc_result = subprocess.run(
            ["xprop", "-id", str(window_id), "WM_CLASS"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
        # WM_CLASS(STRING) = "WeChatAppEx", "WeChatAppEx"
        # Extract class names from format: WM_CLASS(STRING) = "class1", "class2"
        line = proc_result.stdout.lower()
        result = False
        if '"' in line:
            # Parse quoted strings
            import re
            classes = re.findall(r'"([^"]+)"', line)
            result = any(cls in _ELECTRON_WM_CLASSES for cls in classes)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        result = False

    # Store in cache
    _WINDOW_DETECTION_CACHE[window_id] = (result, current_time)

    # Enforce cache size limit (simple LRU: remove oldest entries)
    if len(_WINDOW_DETECTION_CACHE) > _CACHE_MAX_SIZE:
        # Remove oldest 10% of entries
        sorted_items = sorted(_WINDOW_DETECTION_CACHE.items(), key=lambda x: x[1][1])
        for wid, _ in sorted_items[:_CACHE_MAX_SIZE // 10]:
            del _WINDOW_DETECTION_CACHE[wid]

    return result


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


def _get_clipboard_text() -> str:
    if which("wl-paste"):
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return ""
    if which("xsel"):
        try:
            result = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return ""
    if which("xclip"):
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return ""
    try:
        import tkinter as tk
    except ModuleNotFoundError:
        return ""
    try:
        root = tk.Tk()
        root.withdraw()
        text = root.clipboard_get()  # type: ignore[no-untyped-call]
        root.destroy()
        return str(text)
    except Exception:
        return ""


def _start_transient_clipboard_owner(text: str) -> subprocess.Popen[str] | None:
    if not which("xsel"):
        return None
    try:
        proc = subprocess.Popen(
            ["xsel", "--clipboard", "--input", "--nodetach"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CommitError(f"failed to start xsel clipboard owner: {exc}") from exc

    try:
        assert proc.stdin is not None
        proc.stdin.write(text)
        proc.stdin.close()
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            pass
        raise
    return proc


def _stop_transient_clipboard_owner(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=0.2)
        except Exception:
            pass


def _xdotool_key(shortcut: str, *, window_id: int | None = None) -> None:
    """Send a key combination via xdotool."""
    token = shortcut.replace(" ", "").replace("_", "+")
    xdotool_key = token.replace("insert", "Insert")
    if window_id is not None:
        _xdotool_focus_window(window_id)
        time.sleep(0.15)  # Electron apps need more time to transfer focus to input field
    cmd = ["xdotool", "key", "--clearmodifiers", xdotool_key]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise CommitError("xdotool not found") from exc
    except subprocess.CalledProcessError as exc:
        raise CommitError(f"xdotool key failed: {exc}") from exc


def _xdotool_focus_window(window_id: int) -> None:
    # Focus the target window first; --window alone is ignored by many apps.
    try:
        subprocess.run(
            ["xdotool", "windowfocus", "--sync", str(window_id)],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

def _run_command_with_input(cmd: list[str], text: str) -> None:
    try:
        subprocess.run(cmd, input=text, text=True, capture_output=True, check=True)
    except FileNotFoundError as exc:
        raise CommitError(f"command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise CommitError(f"command failed: {cmd} {detail}") from exc
