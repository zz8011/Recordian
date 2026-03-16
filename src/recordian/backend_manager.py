from __future__ import annotations

import atexit
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

# 全局进程注册表
_ACTIVE_BACKEND_PROCESSES: list[subprocess.Popen[str]] = []
_ORPHAN_RECORDER_PATH_TOKEN = "/tmp/recordian-ptt-"


def _is_recordian_recorder_command(command: str) -> bool:
    normalized = str(command).strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    return (
        "ffmpeg" in lowered
        and "-f pulse" in lowered
        and "pipe:1" in lowered
        and _ORPHAN_RECORDER_PATH_TOKEN in normalized
    )


def _list_orphan_recordian_recorder_pids(*, exclude_pids: set[int] | None = None) -> list[int]:
    excluded = set(exclude_pids or set())
    excluded.add(os.getpid())
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    pids: list[int] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid in excluded:
            continue
        if _is_recordian_recorder_command(command):
            pids.append(pid)
    return pids


def _terminate_backend_process(proc: subprocess.Popen[str], *, timeout_s: float = 2.0) -> None:
    if proc.poll() is not None:
        return

    pgid: int | None = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    try:
        if isinstance(pgid, int) and pgid > 0:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=timeout_s)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if isinstance(pgid, int) and pgid > 0:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            pass


def _cleanup_orphan_recordian_recorders(*, exclude_pids: set[int] | None = None) -> int:
    pids = _list_orphan_recordian_recorder_pids(exclude_pids=exclude_pids)
    if not pids:
        return 0

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            continue

    deadline = time.monotonic() + 1.5
    survivors = set(pids)
    while survivors and time.monotonic() < deadline:
        for pid in list(survivors):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                survivors.discard(pid)
            except OSError:
                survivors.discard(pid)
        if survivors:
            time.sleep(0.05)

    for pid in list(survivors):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            continue
    return len(pids)


def _cleanup_backend_processes() -> None:
    """清理所有后端进程"""
    for proc in _ACTIVE_BACKEND_PROCESSES[:]:
        if proc.poll() is None:
            _terminate_backend_process(proc)
        _ACTIVE_BACKEND_PROCESSES.remove(proc)


# 注册清理函数
atexit.register(_cleanup_backend_processes)


def parse_backend_event_line(line: str) -> dict[str, object] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "event" in obj:
        return obj
    return None


class BackendManager:
    """后端进程管理器：负责启动、停止、读取事件"""

    def __init__(
        self,
        config_path: Path,
        events: queue.Queue[dict[str, object]],
        on_state_change: Callable[[bool, str, str], None],
        on_menu_update: Callable[[], None],
    ) -> None:
        self.config_path = config_path
        self._events = events
        self._on_state_change = on_state_change
        self._on_menu_update = on_menu_update
        self.proc: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []

    def _cmd(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "recordian.hotkey_dictate",
            "--config-path",
            str(self.config_path),
            "--notify-backend",
            "none",
        ]

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        cleaned = _cleanup_orphan_recordian_recorders()
        if cleaned:
            self._events.put({"event": "log", "message": f"cleaned_orphan_recorders:{cleaned}"})
        cmd = self._cmd()
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        _ACTIVE_BACKEND_PROCESSES.append(self.proc)
        self._on_state_change(True, "starting", "Starting backend...")
        self._on_menu_update()

        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        t_out = threading.Thread(target=self._read_stream, args=(self.proc.stdout, False), daemon=True)
        t_err = threading.Thread(target=self._read_stream, args=(self.proc.stderr, True), daemon=True)
        t_wait = threading.Thread(target=self._wait, daemon=True)
        self._threads = [t_out, t_err, t_wait]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            _terminate_backend_process(proc)
        # 从注册表移除
        if proc in _ACTIVE_BACKEND_PROCESSES:
            _ACTIVE_BACKEND_PROCESSES.remove(proc)
        self.proc = None
        self._events.put({"event": "stopped"})

    def restart(self) -> None:
        self.stop()
        self.start()

    def _read_stream(self, stream, is_stderr: bool) -> None:  # noqa: ANN001
        for raw in iter(stream.readline, ""):
            event = parse_backend_event_line(raw)
            if event is not None:
                self._events.put(event)
            elif is_stderr:
                text = raw.strip()
                if text:
                    self._events.put({"event": "log", "message": text})
        stream.close()

    def _wait(self) -> None:
        proc = self.proc
        if proc is None:
            return
        code = proc.wait()
        self._events.put({"event": "backend_exited", "code": code})
