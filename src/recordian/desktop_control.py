"""User-local control socket for compositor-owned Wayland hotkeys.

The existing hotkey state machine receives the same press/release edges as
pynput. The compositor owns the actual bindings; no global key snooping is
needed. Requests are acknowledged once and never retried automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import time
from pathlib import Path


def socket_path() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "recordian" / "control.sock"


class ControlServer:
    def __init__(self, on_press, on_release, trigger_keys, toggle_keys, on_exit, *, path=None, get_status=None):
        self.path = Path(path) if path else socket_path()
        self.on_press, self.on_release = on_press, on_release
        self.trigger_keys, self.toggle_keys = sorted(trigger_keys), sorted(toggle_keys)
        self.on_exit = on_exit
        self.get_status = get_status or (lambda: "idle")
        self.server = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Recordian control directory must be owned by this user with mode 0700")
        # The daemon's existing flock guarantees a single owner. A socket left
        # by a previous crash may be removed; never replace a regular file.
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Unexpected file at Recordian control socket path")
            self.path.unlink()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.path.chmod(0o600)
        self.server.listen(8)
        self.server.settimeout(0.1)
        return self

    def dispatch(self, action: str) -> str:
        if action == "ping":
            return "ready"
        if action == "status":
            return self.get_status()
        if action == "toggle":
            if not self.toggle_keys:
                return "error: hotkey not configured"
            try:
                for key in self.toggle_keys:
                    self.on_press(key)
            finally:
                for key in self.toggle_keys:
                    self.on_release(key)
            return "ok"
        if action == "exit":
            self.on_exit()
            return "ok"
        if action not in {"ptt-press", "ptt-release", "toggle-press", "toggle-release"}:
            return "error: unknown action"
        keys = self.trigger_keys if action.startswith("ptt-") else self.toggle_keys
        if not keys:
            return "error: hotkey not configured"
        callback = self.on_press if action.endswith("-press") else self.on_release
        for key in keys:
            callback(key)
        return "ok"

    def poll(self):
        try:
            conn, _ = self.server.accept()
        except TimeoutError:
            return
        with conn:
            conn.settimeout(0.5)
            try:
                data = b""
                while b"\n" not in data and len(data) <= 64:
                    part = conn.recv(65 - len(data))
                    if not part:
                        break
                    data += part
                if not data.endswith(b"\n") or len(data) > 64:
                    reply = "error: invalid request"
                else:
                    reply = self.dispatch(data[:-1].decode("ascii"))
                conn.sendall((reply + "\n").encode())
            except (OSError, UnicodeError):
                return

    def __exit__(self, *exc):
        if self.server:
            self.server.close()
        self.path.unlink(missing_ok=True)


def send_action(action: str, *, path=None) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path or socket_path()))
        client.sendall((action + "\n").encode("ascii"))
        reply = client.recv(256).decode().strip()
        allowed = {"idle", "recording", "transcribing", "stopped"} if action == "status" else {"ok", "ready"}
        if reply not in allowed:
            raise RuntimeError(reply or "No acknowledgement; action outcome unknown")
        return reply


def main():
    parser = argparse.ArgumentParser(description="Control the running Recordian desktop daemon")
    parser.add_argument("action", choices=["ping", "status", "watch-status", "toggle", "ptt-press", "ptt-release", "toggle-press", "toggle-release", "exit"])
    args = parser.parse_args()
    if args.action == "watch-status":
        previous = None
        try:
            while True:
                try:
                    status = send_action("status")
                except (OSError, RuntimeError):
                    status = "stopped"
                if status != previous:
                    print(json.dumps({"alt": status, "class": status}), flush=True)
                    previous = status
                time.sleep(0.5)
        except (KeyboardInterrupt, BrokenPipeError):
            return
    try:
        print(send_action(args.action))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Recordian is unavailable or did not acknowledge the action: {exc}\n")


if __name__ == "__main__":
    main()
