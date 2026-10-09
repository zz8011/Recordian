"""Pipe-bound lifetime for backend children launched by the tray.

Executed by file path in a fresh interpreter: no Recordian imports or threads
may run before the watchdog fork. The launcher then execs the backend in place.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class ParentEndpoint:
    """One launch's CLOEXEC writer; concurrent closes cannot close a reused fd."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


def spawn_backend(cmd: list[str]) -> tuple[subprocess.Popen[str], ParentEndpoint]:
    read_fd, write_fd = os.pipe()  # Python creates both ends non-inheritable.
    endpoint = ParentEndpoint(write_fd)
    try:
        proc = subprocess.Popen(
            # Disable site/user hooks before forking the single-threaded helper.
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), str(read_fd), *cmd],
            pass_fds=(read_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except BaseException:
        endpoint.close()
        raise
    finally:
        os.close(read_fd)
    return proc, endpoint


def _watch_parent(read_fd: int) -> None:
    # Stay in the launch's group. Our membership keeps its identity alive even
    # after the backend exits, so escalation cannot target a recycled PGID.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.set_inheritable(read_fd, False)
    max_fd = int(os.sysconf("SC_OPEN_MAX"))
    os.closerange(0, read_fd)
    os.closerange(read_fd + 1, max_fd)
    try:
        while os.read(read_fd, 1):
            pass
    finally:
        os.close(read_fd)
        # This process can only signal the isolated group it belongs to.
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(2.0)
        os.killpg(os.getpgrp(), signal.SIGKILL)


def _main() -> None:
    # Fail without signalling anything if invoked outside spawn_backend's new
    # session. In particular, never signal a caller's shell or pytest group.
    if os.getsid(0) != os.getpid() or os.getpgrp() != os.getpid():
        raise SystemExit("backend lifecycle launcher requires its own session")
    read_fd = int(sys.argv[1])
    cmd = sys.argv[2:]
    os.set_inheritable(read_fd, False)
    watchdog_pid = os.fork()
    if watchdog_pid == 0:
        try:
            _watch_parent(read_fd)
        finally:
            os._exit(1)
    os.close(read_fd)
    # exec preserves the Popen PID and direct SIGUSR1 delivery to the daemon.
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    _main()
