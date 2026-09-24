"""Hotkey listener edges for toggle / same-key / duration-limit state.

Drives recordian.hotkey_dictate._main_impl with a fake pynput listener and a
fake controller. The controller keeps the dictation lock from a successful
start until complete_processing(), and emits stop events through the on_state
callback main passed in. Tests press the wired on_press/on_release and, for
overlay stop, the SIGUSR1 handler main installed.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import types
from pathlib import Path

import pytest

from recordian.hotkey_dictate import _main_impl


def _join_stop_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "recordian-stop-recording" and thread is not threading.current_thread():
            thread.join(timeout=1)


class _Session:
    def __init__(self, keyboard: object, captured: dict[str, object], calls: dict[str, int], controller: object, restore: object) -> None:
        self.keyboard = keyboard
        self.captured = captured
        self.calls = calls
        self.controller = controller
        self._restore = restore

    def close(self) -> None:
        restore = self._restore
        assert callable(restore)
        restore()
        _join_stop_threads()


def _drive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_args: list[str]) -> _Session:
    captured: dict[str, object] = {}
    calls = {"start": 0, "accepted": 0, "stop": 0, "noop_stop": 0}
    stop_event = threading.Event()
    # Lock is held from an accepted start until postprocess completes.
    # recording is the live capture; busy stays true through processing.
    phase = {"recording": False, "busy": False}

    def fake_build(**kwargs: object):
        on_state = kwargs["on_state"]
        on_error = kwargs["on_error"]
        on_result = kwargs["on_result"]
        on_busy = kwargs["on_busy"]
        assert callable(on_state) and callable(on_error) and callable(on_result) and callable(on_busy)
        captured["on_state"] = on_state
        captured["on_error"] = on_error
        captured["on_result"] = on_result
        captured["on_busy"] = on_busy

        def start_recording(source: str | None = None) -> bool:
            del source
            calls["start"] += 1
            if phase["busy"] or phase["recording"]:
                on_busy({"event": "busy", "reason": "dictation_in_progress"})
                return False
            phase["recording"] = True
            phase["busy"] = True
            calls["accepted"] += 1
            on_state({"event": "recording_started"})
            return True

        def stop_recording() -> bool:
            calls["stop"] += 1
            if not phase["recording"]:
                # Already consumed (duration limit or a previous stop).
                # Do not emit processing_started. Lock state is unchanged.
                calls["noop_stop"] += 1
                return False
            phase["recording"] = False
            on_state({"event": "processing_started"})
            return True

        def emit_duration_limit() -> None:
            if not phase["recording"]:
                raise AssertionError("duration limit without a live recording")
            phase["recording"] = False
            on_state({"event": "recording_duration_limit", "limit_s": 25})
            on_state({"event": "processing_started", "record_backend": "fake"})

        def fail_duration_stop() -> None:
            # stop_record_process failed after the limit event: idle, lock released, no processing_started.
            if not phase["recording"]:
                raise AssertionError("failed stop without a live recording")
            phase["recording"] = False
            on_state({"event": "recording_duration_limit", "limit_s": 25})
            phase["busy"] = False
            on_error({"event": "error", "error": "stop failed"})

        def complete_processing() -> None:
            if phase["recording"]:
                raise AssertionError("processing completed while still recording")
            if not phase["busy"]:
                return
            phase["busy"] = False
            on_result({"event": "result", "result": {"text": ""}})

        def exit_daemon() -> None:
            stop_event.set()

        captured["emit_duration_limit"] = emit_duration_limit
        captured["fail_duration_stop"] = fail_duration_stop
        captured["complete_processing"] = complete_processing
        return start_recording, stop_recording, exit_daemon, stop_event

    keyboard = types.ModuleType("pynput.keyboard")

    class Key:
        def __init__(self, name: str) -> None:
            self.name = name

    class KeyCode:
        def __init__(self, char: str | None = None, vk: int | None = None) -> None:
            self.char = char
            self.vk = vk

    class Listener:
        def __init__(self, on_press: object, on_release: object) -> None:
            captured["on_press"] = on_press
            captured["on_release"] = on_release

        def __enter__(self) -> Listener:
            stop_event.set()
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            del exc_type, exc, tb
            return False

        def stop(self) -> None:
            stop_event.set()

    keyboard.Key = Key  # type: ignore[attr-defined]
    keyboard.KeyCode = KeyCode  # type: ignore[attr-defined]
    keyboard.Listener = Listener  # type: ignore[attr-defined]
    pynput_mod = types.ModuleType("pynput")
    pynput_mod.keyboard = keyboard  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pynput", pynput_mod)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", keyboard)
    monkeypatch.setattr("recordian.hotkey_dictate.build_ptt_hotkey_handlers", fake_build)

    config_path = tmp_path / "hotkey.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recordian-hotkey-dictate",
            "--no-load-config",
            "--config-path",
            str(config_path),
            "--notify-backend",
            "none",
            "--exit-hotkey",
            "",
            "--no-warmup",
            *extra_args,
        ],
    )
    previous = signal.getsignal(signal.SIGUSR1)
    _main_impl()

    def restore() -> None:
        signal.signal(signal.SIGUSR1, previous)

    return _Session(keyboard, captured, calls, captured, restore)


def _tap(captured: dict[str, object], key: object) -> None:
    on_press = captured["on_press"]
    on_release = captured["on_release"]
    assert callable(on_press) and callable(on_release)
    on_press(key)
    on_release(key)
    _join_stop_threads()


def _call(captured: dict[str, object], name: str) -> None:
    fn = captured[name]
    assert callable(fn)
    fn()


def test_ptt_right_alt_press_release_twice_starts_once_and_stops_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "ptt", "--hotkey", "<shift_r>", "--toggle-hotkey", "<alt_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        alt = session.keyboard.Key("alt_r")
        _tap(session.captured, alt)
        _tap(session.captured, alt)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 1, "noop_stop": 0}
    finally:
        session.close()


def test_toggle_same_ctrl_r_starts_then_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "toggle", "--hotkey", "<ctrl_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        ctrl = session.keyboard.Key("ctrl_r")
        _tap(session.captured, ctrl)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 0, "noop_stop": 0}
        _tap(session.captured, ctrl)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 1, "noop_stop": 0}
    finally:
        session.close()


def test_dedicated_stop_does_not_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "toggle", "--hotkey", "<ctrl_r>", "--stop-hotkey", "<alt_r>"],
    )
    try:
        ctrl = session.keyboard.Key("ctrl_r")
        alt = session.keyboard.Key("alt_r")
        _tap(session.captured, alt)
        assert session.calls == {"start": 0, "accepted": 0, "stop": 0, "noop_stop": 0}
        _tap(session.captured, ctrl)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 0, "noop_stop": 0}
        _tap(session.captured, alt)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 1, "noop_stop": 0}
    finally:
        session.close()


def test_ptt_right_ctrl_stop_does_not_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "ptt", "--hotkey", "<shift_r>", "--toggle-hotkey", "<alt_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        alt = session.keyboard.Key("alt_r")
        ctrl = session.keyboard.Key("ctrl_r")
        _tap(session.captured, ctrl)
        assert session.calls == {"start": 0, "accepted": 0, "stop": 0, "noop_stop": 0}
        _tap(session.captured, alt)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 0, "noop_stop": 0}
        _tap(session.captured, ctrl)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 1, "noop_stop": 0}
    finally:
        session.close()


def test_duration_processing_rejects_until_complete_then_fresh_press_starts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "ptt", "--hotkey", "<shift_r>", "--toggle-hotkey", "<alt_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        alt = session.keyboard.Key("alt_r")
        _tap(session.captured, alt)
        _call(session.captured, "emit_duration_limit")
        _tap(session.captured, alt)
        assert session.calls == {"start": 2, "accepted": 1, "stop": 0, "noop_stop": 0}
        _call(session.captured, "complete_processing")
        _tap(session.captured, alt)
        assert session.calls == {"start": 3, "accepted": 2, "stop": 0, "noop_stop": 0}
    finally:
        session.close()


def test_held_key_does_not_restart_until_fresh_press_after_complete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "ptt", "--hotkey", "<shift_r>", "--toggle-hotkey", "<alt_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        alt = session.keyboard.Key("alt_r")
        on_press = session.captured["on_press"]
        on_release = session.captured["on_release"]
        assert callable(on_press) and callable(on_release)
        on_press(alt)
        on_press(alt)
        assert session.calls == {"start": 1, "accepted": 1, "stop": 0, "noop_stop": 0}
        _call(session.captured, "emit_duration_limit")
        on_press(alt)
        assert session.calls["start"] == 1
        assert session.calls["accepted"] == 1
        on_release(alt)
        _join_stop_threads()
        assert session.calls["start"] == 1
        _call(session.captured, "complete_processing")
        assert session.calls["accepted"] == 1
        on_press(alt)
        on_release(alt)
        assert session.calls == {"start": 2, "accepted": 2, "stop": 0, "noop_stop": 0}
    finally:
        session.close()


def test_duration_error_before_processing_allows_fresh_press(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "toggle", "--hotkey", "<ctrl_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        ctrl = session.keyboard.Key("ctrl_r")
        _tap(session.captured, ctrl)
        _call(session.captured, "fail_duration_stop")
        _tap(session.captured, ctrl)
        assert session.calls == {"start": 2, "accepted": 2, "stop": 0, "noop_stop": 0}
    finally:
        session.close()


def test_overlay_sigusr1_then_processing_then_fresh_alt_starts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "ptt", "--hotkey", "<shift_r>", "--toggle-hotkey", "<alt_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        alt = session.keyboard.Key("alt_r")
        _tap(session.captured, alt)
        os.kill(os.getpid(), signal.SIGUSR1)
        _join_stop_threads()
        assert session.calls["stop"] == 1
        assert session.calls["accepted"] == 1
        _tap(session.captured, alt)
        assert session.calls == {"start": 2, "accepted": 1, "stop": 1, "noop_stop": 0}
        _call(session.captured, "complete_processing")
        _tap(session.captured, alt)
        assert session.calls == {"start": 3, "accepted": 2, "stop": 1, "noop_stop": 0}
    finally:
        session.close()


def test_noop_keyup_does_not_poison_next_async_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _drive(
        monkeypatch,
        tmp_path,
        ["--trigger-mode", "ptt", "--hotkey", "<shift_r>", "--toggle-hotkey", "<alt_r>", "--stop-hotkey", "<ctrl_r>"],
    )
    try:
        alt = session.keyboard.Key("alt_r")
        shift = session.keyboard.Key("shift_r")
        on_press = session.captured["on_press"]
        on_release = session.captured["on_release"]
        assert callable(on_press) and callable(on_release)

        _tap(session.captured, alt)
        _call(session.captured, "emit_duration_limit")
        # PTT keyup after the limit: recording is already gone, stop returns false.
        on_press(shift)
        on_release(shift)
        _join_stop_threads()
        assert session.calls["noop_stop"] == 1
        assert session.calls["accepted"] == 1

        _call(session.captured, "complete_processing")
        _tap(session.captured, alt)
        assert session.calls["accepted"] == 2

        _call(session.captured, "emit_duration_limit")
        _tap(session.captured, alt)
        assert session.calls["accepted"] == 2
        assert session.calls["stop"] == 1
        _call(session.captured, "complete_processing")
        _tap(session.captured, alt)
        assert session.calls["accepted"] == 3
        assert session.calls["noop_stop"] == 1
    finally:
        session.close()
