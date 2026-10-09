"""Real, isolated processes for BackendManager parent/child lifetime rules."""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

_DAEMON = r'''
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
mode = sys.argv[2]
signal.signal(signal.SIGUSR1, lambda *_: print(json.dumps({"event": "usr1", "pid": os.getpid()}), flush=True))
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "exit":
    signal.signal(signal.SIGUSR1, lambda *_: sys.exit(7))
if mode == "stubborn-child":
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline() == "ready\n"
print(json.dumps({"event": "ready", "pid": os.getpid()}), flush=True)
while True:
    time.sleep(0.05)
'''

_PARENT = r'''
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from recordian.backend_manager import BackendManager

events = queue.Queue()
manager = BackendManager(Path(sys.argv[2]), events, lambda *_: None, lambda: None)
manager._cmd = lambda: [sys.executable, "-u", sys.argv[1], sys.argv[2], sys.argv[3]]
def start():
    thread = threading.Thread(target=manager.start)
    thread.start()
    thread.join()
def emit():
    print(json.dumps({"pid": manager.proc.pid if manager.proc else None}), flush=True)
start()
emit()
for line in sys.stdin:
    action = line.strip()
    if action == "event":
        print(json.dumps(events.get(timeout=6)), flush=True)
    elif action == "status":
        print(json.dumps({"code": manager.proc.poll()}), flush=True)
    elif action == "start":
        start()
        emit()
    elif action == "stop":
        manager.stop()
        emit()
    elif action == "restart":
        manager.restart()
        emit()
    elif action == "usr1":
        print(json.dumps({"sent": manager.request_stop_recording()}), flush=True)
    elif action == "unrelated":
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "recordian.hotkey_dictate"],
            close_fds=False, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(json.dumps({"pid": unrelated.pid}), flush=True)
    elif action == "streams-closed":
        for thread in manager._threads:
            thread.join(timeout=2)
        print(json.dumps({"closed": manager.proc.stdout.closed and manager.proc.stderr.closed}), flush=True)
    elif action == "exit":
        break
'''


def _eventually(check, *, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return bool(check())


class _Family:
    def __init__(self, tmp_path: Path, mode: str) -> None:
        self.lock = tmp_path / "hotkey-dictate.lock"
        daemon = tmp_path / "isolated_daemon.py"
        daemon.write_text(_DAEMON)
        self.handles: dict[int, int] = {}
        self.parent = subprocess.Popen(
            [sys.executable, "-u", "-c", _PARENT, str(daemon), str(self.lock), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    def read(self) -> dict:
        assert self.parent.stdout is not None
        ready, _, _ = select.select([self.parent.stdout], [], [], 8)
        assert ready, "isolated parent did not respond"
        line = self.parent.stdout.readline()
        assert line, "isolated parent exited before responding"
        return json.loads(line)

    def ask(self, action: str) -> dict:
        assert self.parent.stdin is not None
        self.parent.stdin.write(action + "\n")
        self.parent.stdin.flush()
        return self.read()

    def own(self, pid: int) -> None:
        # Capture identity while the child is known to be alive. Teardown uses
        # pidfds, never signals a potentially reused numeric PID.
        self.handles[pid] = os.pidfd_open(pid)
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        for child in children:
            child_pid = int(child)
            self.handles[child_pid] = os.pidfd_open(child_pid)

    def dead(self, pid: int) -> bool:
        return bool(select.select([self.handles[pid]], [], [], 0)[0])

    def unlocked(self) -> bool:
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd)

    def ready(self) -> int:
        pid = self.read()["pid"]
        event = self.ask("event")
        assert event == {"event": "ready", "pid": pid}
        self.own(pid)
        assert not self.unlocked()
        return pid

    def close(self) -> None:
        if self.parent.poll() is None:
            self.parent.kill()
        self.parent.wait(timeout=8)
        for fd in self.handles.values():
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid, fd in self.handles.items():
            select.select([fd], [], [], 8)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(fd)
        for stream in (self.parent.stdin, self.parent.stdout, self.parent.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def family(tmp_path):
    # This pytest process alone adopts and reaps its isolated grandchildren.
    # PR_SET_CHILD_SUBREAPER is unrelated to PDEATHSIG or production settings.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    families = []

    def create(mode="normal"):
        instance = _Family(tmp_path, mode)
        families.append(instance)
        return instance

    def restore_subreaper():
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0

    try:
        yield create
    finally:
        # All callbacks run even if one close raises; restoration runs last.
        with ExitStack() as cleanup:
            cleanup.callback(restore_subreaper)
            for instance in families:
                cleanup.callback(instance.close)


@pytest.mark.parametrize("mode", ["normal", "ignore-term"])
def test_parent_sigkill_releases_child_lock_and_leaves_unrelated_alive(family, mode):
    instance = family(mode)
    pid = instance.ready()
    unrelated_pid = instance.ask("unrelated")["pid"]
    instance.own(unrelated_pid)
    instance.parent.kill()
    assert instance.parent.wait(timeout=5) == -signal.SIGKILL
    assert _eventually(lambda: instance.dead(pid)), "owned child survived parent SIGKILL"
    assert _eventually(instance.unlocked), "daemon lock remained held"
    # It was spawned by the same parent with close_fds=False: CLOEXEC must
    # prevent it from retaining the writer, and its separate group must survive.
    assert not instance.dead(unrelated_pid)
    replacement = family()
    replacement.ready()


def test_spawn_thread_exit_does_not_kill_child_and_usr1_keeps_backend_pid(family):
    instance = family()
    pid = instance.ready()
    # The spawn thread has joined before the parent emitted its PID.
    time.sleep(0.2)
    assert instance.ask("status") == {"code": None}
    assert not instance.dead(pid)
    assert instance.ask("usr1") == {"sent": True}
    assert instance.ask("event") == {"event": "usr1", "pid": pid}


def test_normal_start_stop_restart_and_parent_exit(family):
    instance = family()
    old_pid = instance.ready()
    assert instance.ask("start") == {"pid": old_pid}
    assert instance.ask("stop") == {"pid": None}
    assert _eventually(lambda: instance.dead(old_pid))
    assert instance.unlocked()
    new_pid = instance.ask("start")["pid"]
    assert new_pid != old_pid
    assert _eventually(lambda: instance.ask("event").get("event") == "ready")
    instance.own(new_pid)
    latest_pid = instance.ask("restart")["pid"]
    assert latest_pid != new_pid
    assert _eventually(lambda: instance.dead(new_pid))
    assert _eventually(lambda: instance.ask("event").get("event") == "ready")
    instance.own(latest_pid)
    assert instance.parent.stdin is not None
    instance.parent.stdin.write("exit\n")
    instance.parent.stdin.flush()
    assert instance.parent.wait(timeout=5) == 0
    assert _eventually(lambda: instance.dead(latest_pid))
    assert instance.unlocked()


def test_child_exit_releases_lock_and_does_not_hold_output_pipes(family):
    instance = family("exit")
    instance.ready()
    assert instance.ask("usr1") == {"sent": True}
    assert instance.ask("event") == {"event": "backend_exited", "code": 7, "intentional": False}
    assert instance.unlocked()
    assert instance.ask("streams-closed") == {"closed": True}
    assert _eventually(lambda: all(instance.dead(pid) for pid in instance.handles))
    assert instance.parent.poll() is None  # EOF came from child-exit cleanup.


@pytest.mark.parametrize("failure", ["none", "body", "cleanup"])
def test_family_restores_subreaper_even_on_errors(tmp_path, monkeypatch, failure):
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    fixture = family.__wrapped__(tmp_path)
    create = next(fixture)
    try:
        if failure == "cleanup":
            class BrokenFamily:
                def __init__(self, *_):
                    pass

                def close(self):
                    raise RuntimeError("cleanup failure")

            monkeypatch.setattr(sys.modules[__name__], "_Family", BrokenFamily)
            create()
            with pytest.raises(RuntimeError, match="cleanup failure"):
                fixture.close()
        elif failure == "body":
            with pytest.raises(RuntimeError, match="body failure"):
                fixture.throw(RuntimeError("body failure"))
        else:
            fixture.close()
    finally:
        fixture.close()
        current = ctypes.c_int()
        assert libc.prctl(37, ctypes.byref(current), 0, 0, 0) == 0
        assert current.value == previous.value


def test_watchdog_finishes_own_group_cleanup_even_when_backend_exits_first(family):
    instance = family("stubborn-child")
    pid = instance.ready()
    descendants = set(instance.handles) - {pid}
    assert descendants, "synthetic daemon did not create its stubborn child"
    instance.parent.kill()
    instance.parent.wait(timeout=5)
    assert _eventually(lambda: all(instance.dead(child) for child in instance.handles))
    assert instance.unlocked()
