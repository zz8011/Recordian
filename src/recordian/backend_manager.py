from __future__ import annotations

import atexit
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

# 全局进程注册表
_ACTIVE_BACKEND_PROCESSES: list[subprocess.Popen[str]] = []


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
        # 标记「我们主动停的后端」，用于区分崩溃/被误退出与正常停止，
        # 让托盘只在非预期退出时自动重启。
        self._intentional_stop = False

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
        self._intentional_stop = False
        # Do not adopt processes from the system process list. UID, argv, config
        # and even PPID=1 cannot prove that another backend belongs to us or is
        # abandoned; recorder argv has no config identity. Only manage children
        # launched here and retained as Popen objects in our process registry.
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
        t_wait = threading.Thread(target=self._wait, args=(self.proc,), daemon=True)
        self._threads = [t_out, t_err, t_wait]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        self._intentional_stop = True
        if proc.poll() is None:
            _terminate_backend_process(proc)
        # 从注册表移除
        if proc in _ACTIVE_BACKEND_PROCESSES:
            _ACTIVE_BACKEND_PROCESSES.remove(proc)
        self.proc = None
        self._events.put({"event": "stopped"})

    def request_stop_recording(self) -> bool:
        """请求后端停止当前录音（overlay 点击停止）。

        通过 SIGUSR1 通知后端进程；后端在 hotkey_dictate 中注册了处理器，
        收到信号后走与松开热键相同的停止流程。仅发给后端主进程，不广播
        进程组（避免 SIGUSR1 默认动作误杀 ffmpeg 等子进程）。
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            os.kill(proc.pid, signal.SIGUSR1)
        except OSError:
            return False
        return True

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

    def _wait(self, proc: subprocess.Popen[str] | None = None) -> None:
        target = proc if proc is not None else self.proc
        if target is None:
            return
        code = target.wait()
        # 「主动停止」有两种：用户点了停止后端，或我们自己 restart（此时 self.proc
        # 已经换成新进程）。只有进程自己退出且没被替换，才算非预期退出。
        intentional = self._intentional_stop or target is not self.proc
        self._events.put(
            {
                "event": "backend_exited",
                "code": code,
                "intentional": intentional,
            }
        )
