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


# ---------------------------------------------------------------------------
# Continuous partial failure stays visible (controller on_error)
# ---------------------------------------------------------------------------

import argparse  # noqa: E402
import io  # noqa: E402

from recordian.hotkey_dictate import build_ptt_hotkey_handlers  # noqa: E402
from recordian.linux_dictate import RecordProcessHandle  # noqa: E402
from recordian.realtime_asr import _RealtimeASRWorkerHandle  # noqa: E402


class _CtlFakeCommitter:
    backend_name = "stdout"
    target_window_id = None

    def commit(self, text: str):  # noqa: ANN001, ANN202
        return types.SimpleNamespace(backend="stdout", committed=True, detail="printed")


class _CtlConfuciusProvider:
    provider_name = "confucius-asr"

    def transcribe_file(self, audio_path, hotwords):  # noqa: ANN001, ANN201, ANN202
        raise AssertionError("pipeline is faked; transcribe_file must not run")


def _continuous_controller(monkeypatch: pytest.MonkeyPatch, worker: _RealtimeASRWorkerHandle):  # noqa: ANN202
    """build_ptt_hotkey_handlers with every side effect faked; returns the
    collected events and the pipeline contexts."""
    events: list[dict[str, object]] = []
    contexts: list[object] = []
    pipeline_done = threading.Event()

    def _fake_start_record_process(**kwargs: object) -> RecordProcessHandle:
        kwargs["output_path"].write_bytes(b"")  # type: ignore[union-attr]
        return RecordProcessHandle(
            process=types.SimpleNamespace(poll=lambda: 0),
            monitor_stream=io.BytesIO(b""),
        )

    monkeypatch.setattr("recordian.recording_controller.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "recordian.recording_controller.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse"
    )
    monkeypatch.setattr(
        "recordian.recording_controller.resolve_committer", lambda backend: _CtlFakeCommitter()
    )
    monkeypatch.setattr(
        "recordian.recording_controller.create_provider", lambda args: _CtlConfuciusProvider()
    )
    monkeypatch.setattr("recordian.recording_controller.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.recording_controller.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.recording_controller.stop_record_process", lambda *a, **k: None)
    monkeypatch.setattr(
        "recordian.recording_controller.start_wake_session_monitor", lambda context: types.SimpleNamespace()
    )
    monkeypatch.setattr(
        "recordian.recording_controller._start_realtime_asr_worker", lambda **kwargs: worker
    )

    def _fake_pipeline(context) -> None:  # noqa: ANN001
        contexts.append(context)
        pipeline_done.set()

    monkeypatch.setattr("recordian.recording_controller.run_postprocess_pipeline", _fake_pipeline)

    args = argparse.Namespace(
        cooldown_ms=0,
        record_backend="ffmpeg-pulse",
        commit_backend="stdout",
        enable_auto_lexicon=False,
        debug_diagnostics=False,
        enable_text_refine=False,
        warmup=False,
        record_format="wav",
        input_device="default",
        channels=1,
        sample_rate=16000,
        wake_use_semantic_gate=False,
        wake_owner_verify=False,
        hotword=[],
        auto_hard_enter=False,
        enable_streaming_refine=False,
        enable_streaming_commit=True,
    )
    start, stop, _exit, _stop_event = build_ptt_hotkey_handlers(
        args=args,
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )
    return types.SimpleNamespace(
        start=start, stop=stop, events=events, contexts=contexts, pipeline_done=pipeline_done
    )


def _finished_worker(*, segments: int, outcome: str) -> _RealtimeASRWorkerHandle:
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    worker = _RealtimeASRWorkerHandle(thread=thread, continuous=True)
    worker.segments_committed = segments
    worker.outcome = outcome
    worker.final_text = "第一段。第二段。"
    worker.commit_info = {
        "backend": "fcitx",
        "committed": segments > 0,
        "detail": "continuous_prefix_kept;ime_stale" if segments else "realtime_cancelled",
        "outcome": "committed" if segments else outcome,
        "segments_committed": segments,
    }
    return worker


def test_committed_prefix_failure_emits_visible_error(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _finished_worker(segments=2, outcome="uncertain")
    h = _continuous_controller(monkeypatch, worker)

    assert h.start() is True
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True

    errors = [e for e in h.events if e.get("event") == "error"]
    assert any("continuous_partial_failure" in str(e.get("error", "")) for e in errors), (
        "a failed continuous turn with a committed prefix must surface a visible "
        "error, not masquerade as success"
    )
    # The pipeline still got the preserved-prefix commit_info (no fallback).
    assert h.contexts[0].prefetched_commit_info["committed"] is True
    assert h.contexts[0].prefetched_commit_info["segments_committed"] == 2


def test_clean_continuous_commit_emits_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _finished_worker(segments=2, outcome="committed")
    worker.commit_info["detail"] = "continuous_final"
    h = _continuous_controller(monkeypatch, worker)

    assert h.start() is True
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True

    errors = [e for e in h.events if e.get("event") == "error"]
    assert errors == [], "a fully committed continuous turn is a normal result"


def test_no_segments_failure_needs_no_partial_error(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _finished_worker(segments=0, outcome="cancelled")
    worker.commit_info = {
        "backend": "fcitx",
        "committed": False,
        "detail": "realtime_cancelled",
        "outcome": "cancelled",
        "segments_committed": 0,
    }
    h = _continuous_controller(monkeypatch, worker)

    assert h.start() is True
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True

    errors = [e for e in h.events if e.get("event") == "error"]
    assert not any("continuous_partial_failure" in str(e.get("error", "")) for e in errors)
