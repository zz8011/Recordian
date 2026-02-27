from __future__ import annotations

import atexit
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable


# 全局进程注册表
_ACTIVE_BACKEND_PROCESSES: list[subprocess.Popen[str]] = []


def _cleanup_backend_processes() -> None:
    """清理所有后端进程"""
    for proc in _ACTIVE_BACKEND_PROCESSES[:]:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=0.5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
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
        cmd = self._cmd()
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
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
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
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
