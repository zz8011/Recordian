"""Focused tests for the Confucius recording duration guard.

All fakes: no GPU, no microphone, no real timers, no live processes.
See .runs/20260924-desktop-setup/kimi-duration-guard.design.md for the contract.
"""
from __future__ import annotations

import argparse
import io
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from recordian.duration_guard import (
    CONFUCIUS_RECORDING_LIMIT_S,
    CONFUCIUS_SERVER_SESSION_BUDGET_S,
    clamp_oneshot_duration_s,
    recording_limit_s_for_provider,
)
from recordian.hotkey_dictate import build_hotkey_handlers, build_ptt_hotkey_handlers
from recordian.linux_dictate import DictateResult, RecordProcessHandle
from recordian.realtime_asr import _RealtimeASRWorkerHandle

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeTimer:
    """Manually fired timer. fire() honors cancel(); fire_force() simulates a
    timer thread that had ALREADY started running before cancel() landed."""

    def __init__(self, delay: float, callback) -> None:  # noqa: ANN001
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.started and not self.cancelled:
            self.callback()

    def fire_force(self) -> None:
        self.callback()


class _TimerFactory:
    def __init__(self) -> None:
        self.created: list[_FakeTimer] = []

    def __call__(self, delay: float, callback) -> _FakeTimer:  # noqa: ANN001
        timer = _FakeTimer(delay, callback)
        self.created.append(timer)
        return timer


class _ConfuciusProvider:
    provider_name = "confucius-asr"

    def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
        raise AssertionError("pipeline is faked; transcribe_file must not run")


class _HttpCloudProvider:
    provider_name = "http-cloud"

    def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
        raise AssertionError("pipeline is faked; transcribe_file must not run")


class _FakeCommitter:
    backend_name = "stdout"
    target_window_id = None

    def commit(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(backend="stdout", committed=True, detail="printed")


class _FakeProcess:
    def poll(self) -> int:
        return 0


def _ptt_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "cooldown_ms": 0,
        "record_backend": "ffmpeg-pulse",
        "commit_backend": "stdout",
        "enable_auto_lexicon": False,
        "debug_diagnostics": False,
        "enable_text_refine": False,
        "warmup": False,
        "record_format": "wav",
        "input_device": "default",
        "channels": 1,
        "sample_rate": 16000,
        "wake_use_semantic_gate": False,
        "wake_owner_verify": False,
        "hotword": [],
        "auto_hard_enter": False,
        "enable_streaming_refine": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _make_ptt(monkeypatch, provider, *, start_worker=None, timers=None, args_overrides=None):  # noqa: ANN001, ANN202
    events: list[dict[str, object]] = []
    if timers is None:
        timers = _TimerFactory()
    stops = {"count": 0}
    contexts: list[object] = []
    pipeline_done = threading.Event()

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        kwargs["output_path"].write_bytes(b"")
        return RecordProcessHandle(process=_FakeProcess(), monitor_stream=io.BytesIO(b""))

    def _fake_stop_record_process(*a, **k) -> None:  # noqa: ANN002, ANN003
        stops["count"] += 1

    def _fake_pipeline(context) -> None:  # noqa: ANN001
        contexts.append(context)
        pipeline_done.set()

    def _fake_worker_start(**kwargs):  # noqa: ANN003, ANN202
        if start_worker is not None:
            return start_worker(**kwargs)
        return None

    monkeypatch.setattr("recordian.recording_controller.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.recording_controller.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.recording_controller.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.recording_controller.create_provider", lambda args: provider)
    monkeypatch.setattr("recordian.recording_controller.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.recording_controller.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.recording_controller.stop_record_process", _fake_stop_record_process)
    monkeypatch.setattr("recordian.recording_controller.start_wake_session_monitor", lambda context: SimpleNamespace())
    monkeypatch.setattr("recordian.recording_controller._start_realtime_asr_worker", _fake_worker_start)
    monkeypatch.setattr("recordian.recording_controller.run_postprocess_pipeline", _fake_pipeline)

    start, stop, exit_daemon, stop_event = build_ptt_hotkey_handlers(
        args=_ptt_args(**(args_overrides or {})),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
        timer_factory=timers,
    )
    return SimpleNamespace(
        start=start,
        stop=stop,
        exit_daemon=exit_daemon,
        stop_event=stop_event,
        events=events,
        timers=timers,
        stops=stops,
        contexts=contexts,
        pipeline_done=pipeline_done,
    )


def _wait_next_start(h) -> None:  # noqa: ANN001
    """Wait until the postprocess thread of the previous stop released the lock."""
    for _ in range(200):
        if h.start():
            return
        time.sleep(0.005)
    pytest.fail("previous session lock was not released in time")


def _limit_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in events if e.get("event") == "recording_duration_limit"]


# ---------------------------------------------------------------------------
# Guard constants
# ---------------------------------------------------------------------------

def test_limit_is_below_server_budget() -> None:
    assert CONFUCIUS_RECORDING_LIMIT_S == 25.0
    assert CONFUCIUS_RECORDING_LIMIT_S < CONFUCIUS_SERVER_SESSION_BUDGET_S


def test_recording_limit_only_for_confucius() -> None:
    assert recording_limit_s_for_provider(_ConfuciusProvider()) == CONFUCIUS_RECORDING_LIMIT_S
    assert recording_limit_s_for_provider(_HttpCloudProvider()) is None
    assert recording_limit_s_for_provider(object()) is None


# ---------------------------------------------------------------------------
# PTT / toggle guard behaviour
# ---------------------------------------------------------------------------

def test_confucius_limit_fires_exactly_one_normal_stop(monkeypatch) -> None:
    h = _make_ptt(monkeypatch, _ConfuciusProvider())
    assert h.start() is True

    assert len(h.timers.created) == 1
    timer = h.timers.created[0]
    assert timer.started is True
    assert 0.0 < timer.delay <= CONFUCIUS_RECORDING_LIMIT_S

    timer.fire()
    assert h.pipeline_done.wait(timeout=1.0) is True

    # Exactly one normal stop through the existing path, one visible event.
    assert h.stops["count"] == 1
    assert len(h.contexts) == 1
    limit_events = _limit_events(h.events)
    assert len(limit_events) == 1
    assert limit_events[0]["limit_s"] == CONFUCIUS_RECORDING_LIMIT_S
    assert limit_events[0]["provider"] == "confucius-asr"
    # Event ordering: limit notice precedes the usual processing_started.
    kinds = [e.get("event") for e in h.events]
    assert kinds.index("recording_duration_limit") < kinds.index("processing_started")

    # The user is still holding the key and releases AFTER the auto stop:
    # must be a no-op (no double stop, no second pipeline run).
    assert h.stop() is False
    assert h.stops["count"] == 1

    # The accepted stop cancelled its own timer; even a forced late fire
    # (timer thread already running) is a silent no-op — no second stop,
    # no duplicate notification.
    assert timer.cancelled is True
    timer.fire_force()
    assert h.stops["count"] == 1
    assert len(_limit_events(h.events)) == 1
    time.sleep(0.1)
    assert len(h.contexts) == 1


def test_manual_stop_cancels_timer_and_late_fire_is_noop(monkeypatch) -> None:
    h = _make_ptt(monkeypatch, _ConfuciusProvider())
    assert h.start() is True
    timer = h.timers.created[0]

    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert timer.cancelled is True
    assert h.stops["count"] == 1

    timer.fire()
    timer.fire_force()
    assert h.stops["count"] == 1
    assert _limit_events(h.events) == []


def test_stale_callback_cannot_stop_next_session(monkeypatch) -> None:
    """Deterministic interleaving: the stale timer callback is captured,
    the user stops session 1 manually and starts session 2, THEN the stale
    callback resumes. It must not stop session 2 nor emit a notification."""
    h = _make_ptt(monkeypatch, _ConfuciusProvider())
    assert h.start() is True
    stale_callback = h.timers.created[0].callback

    # Manual stop of session 1 completes; postprocess drains; session 2 starts.
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    _wait_next_start(h)
    assert len(h.timers.created) == 2

    # The stale callback resumes now — after the new recording owns the slot.
    stale_callback()
    assert h.stops["count"] == 1, "stale callback must not stop the new recording"
    assert _limit_events(h.events) == [], "lost race must not emit a false notification"

    # Session 2 is still alive and its OWN limit timer works normally.
    h.timers.created[1].fire()
    assert h.stops["count"] == 2
    assert len(_limit_events(h.events)) == 1
    for _ in range(200):
        if len(h.contexts) >= 2:
            break
        time.sleep(0.005)
    assert len(h.contexts) == 2


def test_non_confucius_provider_arms_no_timer(monkeypatch) -> None:
    h = _make_ptt(monkeypatch, _HttpCloudProvider())
    assert h.start() is True
    assert h.timers.created == []

    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert h.stops["count"] == 1
    assert _limit_events(h.events) == []


def test_timer_armed_only_after_realtime_worker_registered(monkeypatch) -> None:
    timers_seen_at_worker_start: list[int] = []

    def _worker_start(**kwargs):  # noqa: ANN003
        # The timer must not exist yet while the worker is being registered.
        timers_seen_at_worker_start.append(len(timers_holder[0].created))
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join(timeout=1.0)
        return _RealtimeASRWorkerHandle(
            thread=thread,
            final_text="实时终稿",
            detected_language="zh",
            commit_info={"backend": "stdout", "committed": True, "detail": "realtime_ok"},
        )

    timers_holder: list[_TimerFactory] = []

    class _CountingFactory(_TimerFactory):
        def __call__(self, delay: float, callback) -> _FakeTimer:  # noqa: ANN001
            return super().__call__(delay, callback)

    events: list[dict[str, object]] = []
    timers = _CountingFactory()
    timers_holder.append(timers)
    stops = {"count": 0}
    contexts: list[object] = []
    pipeline_done = threading.Event()

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        kwargs["output_path"].write_bytes(b"")
        return RecordProcessHandle(process=_FakeProcess(), monitor_stream=io.BytesIO(b""))

    monkeypatch.setattr("recordian.recording_controller.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.recording_controller.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.recording_controller.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.recording_controller.create_provider", lambda args: _ConfuciusProvider())
    monkeypatch.setattr("recordian.recording_controller.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.recording_controller.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.recording_controller.stop_record_process", lambda *a, **k: stops.__setitem__("count", stops["count"] + 1))
    monkeypatch.setattr("recordian.recording_controller.start_wake_session_monitor", lambda context: SimpleNamespace())
    monkeypatch.setattr("recordian.recording_controller._start_realtime_asr_worker", _worker_start)
    monkeypatch.setattr(
        "recordian.recording_controller.run_postprocess_pipeline",
        lambda context: (contexts.append(context), pipeline_done.set()),
    )

    start, stop, _, _ = build_ptt_hotkey_handlers(
        args=_ptt_args(enable_streaming_commit=True),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
        timer_factory=timers,
    )

    assert start() is True
    assert timers_seen_at_worker_start == [0], "timer must be armed AFTER worker registration"
    assert len(timers.created) == 1

    # The guard-triggered stop joins the registered worker and its final text
    # flows through the normal finalize path (prefetched), proving the stop
    # waited for the worker handle.
    timers.created[0].fire()
    assert pipeline_done.wait(timeout=1.0) is True
    assert stops["count"] == 1
    assert contexts[0].prefetched_asr_text == "实时终稿"
    assert contexts[0].prefetched_commit_info == {"backend": "stdout", "committed": True, "detail": "realtime_ok"}


def test_manual_stop_during_registration_leaves_no_orphan_timer(monkeypatch) -> None:
    """If the user releases the key while the realtime worker is still being
    registered, the stop consumes the recording before the timer is armed —
    the timer must then never be started (no orphan)."""
    holder: dict[str, object] = {}

    def _worker_start(**kwargs):  # noqa: ANN003
        holder["stop"]()  # manual stop lands mid-registration
        return None

    h = _make_ptt(monkeypatch, _ConfuciusProvider(), start_worker=_worker_start)
    holder["stop"] = h.stop

    assert h.start() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert h.stops["count"] == 1
    assert not any(t.started for t in h.timers.created), "orphan timer must never be started"
    for t in h.timers.created:
        t.fire_force()
    assert h.stops["count"] == 1


def test_exit_stops_once_and_cancels_timer(monkeypatch) -> None:
    h = _make_ptt(monkeypatch, _ConfuciusProvider())
    assert h.start() is True
    timer = h.timers.created[0]

    h.exit_daemon()
    assert h.stop_event.is_set() is True
    assert h.stops["count"] == 1
    assert timer.cancelled is True
    timer.fire_force()
    assert h.stops["count"] == 1


# ---------------------------------------------------------------------------
# Registration ownership (grok-duration-review P1)
# ---------------------------------------------------------------------------

def test_concurrent_stop_during_registration_uses_registered_worker(monkeypatch) -> None:
    """Cross-thread, barrier-controlled: the stop thread arrives while the
    worker factory runs inside the registration critical section. The stop
    must serialize BEHIND the registration, snapshot the real returned worker
    handle, and route its final text through the normal postprocess path."""
    factory_entered = threading.Event()
    factory_proceed = threading.Event()
    worker_cancel_event = threading.Event()
    cancel_session_calls: list[int] = []

    def _worker_start(**kwargs):  # noqa: ANN003
        factory_entered.set()
        assert factory_proceed.wait(timeout=5.0)
        thread = threading.Thread(target=lambda: worker_cancel_event.wait(timeout=0.3), daemon=True)
        thread.start()
        return _RealtimeASRWorkerHandle(
            thread=thread,
            final_text="并发终稿",
            detected_language="zh",
            cancel_event=worker_cancel_event,
            cancel_session=lambda: cancel_session_calls.append(1),
        )

    h = _make_ptt(monkeypatch, _ConfuciusProvider(), start_worker=_worker_start)

    start_result: list[bool] = []
    start_errors: list[Exception] = []

    def _start() -> None:
        try:
            start_result.append(h.start())
        except Exception as exc:  # noqa: BLE001
            start_errors.append(exc)

    start_thread = threading.Thread(target=_start, daemon=True)
    start_thread.start()
    assert factory_entered.wait(timeout=2.0) is True

    stop_done: list[bool] = []
    stop_thread = threading.Thread(target=lambda: stop_done.append(h.stop()), daemon=True)
    stop_thread.start()
    time.sleep(0.05)
    assert stop_done == [], "stop must block until the registration critical section finishes"

    factory_proceed.set()
    start_thread.join(timeout=5.0)
    stop_thread.join(timeout=5.0)
    assert h.pipeline_done.wait(timeout=2.0) is True

    assert start_errors == []
    assert start_result == [True]
    assert stop_done == [True]
    assert h.stops["count"] == 1
    # The normal stop path snapshotted the registered worker: its final text
    # reaches the postprocess pipeline (no orphan, no lost handle).
    assert h.contexts[0].prefetched_asr_text == "并发终稿"
    assert cancel_session_calls == [], "worker finished normally inside the join budget"
    assert h.timers.created[0].cancelled is True
    # Lock/state consistent afterwards: a fresh session can start and stop.
    _wait_next_start(h)
    assert h.stop() is True


def test_stop_before_registration_skips_factory_and_keeps_lock_consistent(monkeypatch) -> None:
    """The manual stop consumes the recording BEFORE the registration
    critical section: the worker factory must be skipped entirely (no orphan
    worker), no cleanup/double-release from the start thread, and the lock is
    released exactly once by the postprocess thread."""
    gate_entered = threading.Event()
    gate_open = threading.Event()
    factory_calls: list[int] = []

    def _gated_routing(args):  # noqa: ANN001
        gate_entered.set()
        assert gate_open.wait(timeout=5.0)
        return SimpleNamespace(commit_local=False)

    def _worker_start(**kwargs):  # noqa: ANN003
        factory_calls.append(1)
        return None

    monkeypatch.setattr("recordian.recording_controller.resolve_remote_paste_routing", _gated_routing)
    h = _make_ptt(monkeypatch, _ConfuciusProvider(), start_worker=_worker_start)

    start_result: list[bool] = []
    start_errors: list[Exception] = []

    def _start() -> None:
        try:
            start_result.append(h.start())
        except Exception as exc:  # noqa: BLE001
            start_errors.append(exc)

    start_thread = threading.Thread(target=_start, daemon=True)
    start_thread.start()
    assert gate_entered.wait(timeout=2.0) is True

    # Stop lands before the critical section; postprocess drains and releases
    # the outer lock while the start thread is still gated.
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=2.0) is True
    gate_open.set()
    start_thread.join(timeout=5.0)

    assert start_errors == [], "no double-release RuntimeError from the start thread"
    assert start_result == [True]
    assert factory_calls == [], "factory must be skipped when ownership was already lost"
    assert h.stops["count"] == 1
    assert h.timers.created == []
    # Lock and state are consistent: a fresh recording starts and stops.
    _wait_next_start(h)
    assert h.stop() is True


def test_returned_worker_disposed_when_stop_consumes_state_inside_factory(monkeypatch) -> None:
    """state_lock is an RLock: a same-thread stop fired from inside the
    factory consumes the recording before the factory returns the real
    handle. The handle must NOT be written back into the consumed state slot;
    it must be disposed (cancel_event set, cancel_session called), and the
    postprocess snapshot must see no worker."""
    holder: dict[str, object] = {}
    worker_cancel_event = threading.Event()
    cancel_session_calls: list[int] = []
    factory_calls = {"n": 0}

    def _worker_start(**kwargs):  # noqa: ANN003
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            holder["stop"]()  # reentrant stop while the first factory runs
        thread = threading.Thread(target=lambda: worker_cancel_event.wait(timeout=0.3), daemon=True)
        thread.start()
        return _RealtimeASRWorkerHandle(
            thread=thread,
            final_text="不应被注册",
            cancel_event=worker_cancel_event,
            cancel_session=lambda: cancel_session_calls.append(1),
        )

    h = _make_ptt(monkeypatch, _ConfuciusProvider(), start_worker=_worker_start)
    holder["stop"] = h.stop

    assert h.start() is True
    assert h.pipeline_done.wait(timeout=2.0) is True

    assert h.stops["count"] == 1
    # The returned worker was disposed, never registered into consumed state.
    assert worker_cancel_event.is_set() is True
    assert cancel_session_calls == [1]
    # The stop's snapshot saw no worker → no prefetched text.
    assert h.contexts[0].prefetched_asr_text == ""
    assert not any(t.started for t in h.timers.created)
    # No stale overwrite: a fresh session starts and stops cleanly.
    _wait_next_start(h)
    assert h.stop() is True


def test_stale_start_exception_cannot_corrupt_newer_session(monkeypatch) -> None:
    """Deterministic abort-ownership regression (coordinator's script):
    session1's timer.start() — called OUTSIDE state_lock — first stops
    session1 manually, waits for its pipeline to release the outer lock,
    starts session2 (whose own timer starts normally), and THEN raises
    RuntimeError. Session1's except must see the slot owned by session2's
    handle and do NOTHING: session2 keeps recording, its timer is not
    cancelled, each session's audio temp dir is cleaned exactly once, and the
    propagated error is the original RuntimeError (not a lock-release one).
    Session2 then stops exactly once via its own limit timer."""
    import tempfile

    holder: dict[str, object] = {}
    cleanups: dict[str, int] = {}
    real_td = tempfile.TemporaryDirectory

    class _CountingTD(real_td):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            cleanups[self.name] = 0

        def cleanup(self) -> None:
            cleanups[self.name] += 1
            super().cleanup()

    class _ScriptedTimers(_TimerFactory):
        def __call__(self, delay: float, callback) -> _FakeTimer:  # noqa: ANN001
            timer = _FakeTimer(delay, callback)
            if not self.created:
                def _exploding_start() -> None:
                    timer.started = True
                    # Manual stop consumes session1; its pipeline drains and
                    # releases the outer lock; session2 starts normally.
                    holder["stop"]()
                    assert holder["pipeline_done"].wait(timeout=2.0) is True
                    for _ in range(200):
                        if holder["start"]():
                            break
                        time.sleep(0.005)
                    else:
                        raise AssertionError("session1 lock was not released by its pipeline")
                    raise RuntimeError("timer start failed")

                timer.start = _exploding_start  # type: ignore[method-assign]
            self.created.append(timer)
            return timer

    monkeypatch.setattr("recordian.recording_controller.TemporaryDirectory", _CountingTD)
    timers = _ScriptedTimers()
    h = _make_ptt(monkeypatch, _ConfuciusProvider(), timers=timers)
    holder.update({"stop": h.stop, "start": h.start, "pipeline_done": h.pipeline_done})

    with pytest.raises(RuntimeError, match="timer start failed"):
        h.start()

    # Session1: one manual stop, one pipeline run.
    assert h.stops["count"] == 1
    # Session2 alive and untouched by the stale abort: its timer was never
    # cancelled, and no second recorder stop happened.
    assert len(timers.created) == 2
    session2_timer = timers.created[1]
    assert session2_timer.started is True
    assert session2_timer.cancelled is False

    # Session2 stops exactly once via its OWN limit timer.
    session2_timer.fire()
    assert h.stops["count"] == 2
    assert len(_limit_events(h.events)) == 1
    for _ in range(200):
        if len(h.contexts) >= 2:
            break
        time.sleep(0.005)
    assert len(h.contexts) == 2
    time.sleep(0.1)  # let the postprocess finally run the temp cleanup
    # Each session's audio temp dir was cleaned exactly once — the stale
    # except neither re-cleaned session1's nor touched session2's.
    assert len(cleanups) == 2
    assert all(count == 1 for count in cleanups.values())
    # No error event surfaced from the abort path.
    assert [e for e in h.events if e.get("event") == "error"] == []


# ---------------------------------------------------------------------------
# Oneshot clamp
# ---------------------------------------------------------------------------

def _run_oneshot_with_duration(monkeypatch, asr_provider: str, duration) -> float:  # noqa: ANN001, ANN202
    captured: list[float] = []

    def _fake_run_dictate_once(args):  # noqa: ANN001
        captured.append(args.duration)
        return DictateResult(
            audio_path="/tmp/a.wav",
            record_backend="arecord",
            duration_s=float(args.duration),
            record_latency_ms=1.0,
            transcribe_latency_ms=1.0,
            text="你好",
            commit={"backend": "none", "committed": False, "detail": "disabled"},
        )

    monkeypatch.setattr("recordian.recording_controller.run_dictate_once", _fake_run_dictate_once)
    run_once, _, _ = build_hotkey_handlers(
        args=argparse.Namespace(cooldown_ms=0, asr_provider=asr_provider, duration=duration),
        on_result=lambda payload: None,
        on_error=lambda payload: None,
        on_busy=lambda payload: None,
    )
    run_once()
    for _ in range(200):
        if captured:
            break
        time.sleep(0.005)
    assert len(captured) == 1
    return captured[0]


def test_oneshot_duration_clamped_for_confucius(monkeypatch) -> None:
    assert _run_oneshot_with_duration(monkeypatch, "confucius-asr", 60.0) == CONFUCIUS_RECORDING_LIMIT_S
    assert _run_oneshot_with_duration(monkeypatch, "confucius-asr", None) == CONFUCIUS_RECORDING_LIMIT_S


def test_oneshot_duration_unchanged_within_limit_or_other_provider(monkeypatch) -> None:
    assert _run_oneshot_with_duration(monkeypatch, "confucius-asr", 4.0) == 4.0
    assert _run_oneshot_with_duration(monkeypatch, "http-cloud", 60.0) == 60.0


def test_clamp_oneshot_duration_s_pure() -> None:
    assert clamp_oneshot_duration_s("confucius-asr", None) == 25.0
    assert clamp_oneshot_duration_s("confucius-asr", 30.0) == 25.0
    assert clamp_oneshot_duration_s("confucius-asr", 10.0) == 10.0
    assert clamp_oneshot_duration_s("qwen-asr", None) is None
    assert clamp_oneshot_duration_s("http-cloud", 120.0) == 120.0


# ---------------------------------------------------------------------------
# Tray-visible events (fake TrayApp self — no Tk, no live backend)
# ---------------------------------------------------------------------------

def _make_fake_tray():  # noqa: ANN202
    from recordian.tray_app import TrayApp, UiState

    overlay_calls: list[tuple] = []
    notifications: list[tuple[str, str]] = []
    logs: list[str] = []
    fake = SimpleNamespace(
        state=UiState(),
        overlay=SimpleNamespace(
            set_state=lambda *a: overlay_calls.append(a),
            set_level=lambda *a: None,
        ),
        events=queue.Queue(),
        _off_cue_armed=False,
        _warmup_done=True,
        _auto_restart_attempts=0,
        _cancel_off_cue_timer=lambda: None,
        _cancel_backend_restart=lambda: None,
        _play_global_cue=lambda *a: None,
        _schedule_off_cue_from_overlay=lambda *a, **k: None,
        _update_tray_menu=lambda: None,
        _notify_backend=lambda title, body, **k: notifications.append((title, body)),
        _log_runtime=lambda msg: logs.append(msg),
        _extract_recent_run_observation=TrayApp._extract_recent_run_observation,
        _format_recent_run_log_suffix=TrayApp._format_recent_run_log_suffix,
    )
    return fake, overlay_calls, notifications, logs


def _handle(fake, event: dict[str, object]) -> None:  # noqa: ANN001
    from recordian.tray_app import TrayApp

    TrayApp._handle_event(fake, event)


def test_tray_shows_duration_limit_event() -> None:
    fake, overlay_calls, notifications, logs = _make_fake_tray()
    _handle(fake, {"event": "recording_duration_limit", "limit_s": 25.0, "provider": "confucius-asr"})

    assert "25" in fake.state.detail
    assert "上限" in fake.state.detail
    assert overlay_calls and overlay_calls[-1][0] == "processing"
    assert len(notifications) == 1
    assert "再次按下" in notifications[0][1], "notification must say a fresh key press is needed"
    assert any("recording_duration_limit" in msg for msg in logs)


def test_tray_suppressed_empty_result_is_actionable_failure() -> None:
    fake, overlay_calls, notifications, logs = _make_fake_tray()
    _handle(
        fake,
        {
            "event": "result",
            "result": {
                "text": "",
                "asr_path": "realtime_stale_suppressed",
                "commit": {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": "realtime_asr_timeout_suppressed",
                    "outcome": "uncertain",
                },
            },
        },
    )

    assert fake.state.status == "error"
    assert "本次听写未完成" in fake.state.detail
    assert "No speech detected" not in fake.state.detail
    assert overlay_calls and overlay_calls[-1][0] == "error"
    assert len(notifications) == 1
    assert "听写未完成" in notifications[0][1]
    # Log carries only safe metadata — no transcript, no credentials.
    assert any("result_incomplete" in msg and "realtime_stale_suppressed" in msg for msg in logs)


def test_tray_genuine_no_speech_result_unchanged() -> None:
    fake, overlay_calls, notifications, logs = _make_fake_tray()
    _handle(
        fake,
        {
            "event": "result",
            "result": {
                "text": "",
                "commit": {"backend": "stdout", "committed": False, "detail": "empty_text"},
            },
        },
    )

    assert fake.state.status == "idle"
    assert fake.state.detail == "识别为空"
    assert overlay_calls and overlay_calls[-1] == ("idle", "No speech detected")
    assert notifications == []


def test_tray_committed_result_unchanged() -> None:
    fake, overlay_calls, notifications, logs = _make_fake_tray()
    _handle(
        fake,
        {
            "event": "result",
            "result": {
                "text": "你好",
                "asr_path": "prefetched",
                "commit": {"backend": "stdout", "committed": True, "detail": "printed"},
            },
        },
    )

    assert fake.state.status == "idle"
    assert "你好" in fake.state.detail
    assert overlay_calls and overlay_calls[-1][0] == "idle"
    assert notifications == []


# ---------------------------------------------------------------------------
# Continuous capability vs the 25 s guard
# ---------------------------------------------------------------------------

def _continuous_worker(continuous: bool) -> _RealtimeASRWorkerHandle:  # noqa: ANN202
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    return _RealtimeASRWorkerHandle(thread=thread, continuous=continuous)


def test_confirmed_continuous_worker_skips_duration_limit(monkeypatch) -> None:
    """Once the worker confirmed continuous capability (segments-capable
    composition session + Confucius realtime), the 25 s fallback guard is a
    no-op: rotation enforces the per-socket audio budget instead."""
    h = _make_ptt(
        monkeypatch,
        _ConfuciusProvider(),
        start_worker=lambda **kwargs: _continuous_worker(True),
    )
    assert h.start() is True
    timer = h.timers.created[0]

    timer.fire()
    time.sleep(0.1)
    assert h.stops["count"] == 0, "continuous turn must survive the 25 s guard"
    assert _limit_events(h.events) == [], "no duration warning on a normal internal boundary"

    # A repeated late fire stays a no-op; the user stop still works once.
    timer.fire_force()
    assert h.stops["count"] == 0
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert h.stops["count"] == 1


def test_unconfirmed_worker_does_not_skip_duration_limit(monkeypatch) -> None:
    """A registered worker whose continuous capability was never confirmed
    (old plugin / refused / preview-only) must NOT disarm the guard."""
    h = _make_ptt(
        monkeypatch,
        _ConfuciusProvider(),
        start_worker=lambda **kwargs: _continuous_worker(False),
    )
    assert h.start() is True
    h.timers.created[0].fire()
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert h.stops["count"] == 1
    assert len(_limit_events(h.events)) == 1


def test_continuous_skip_is_bound_to_the_exact_handle(monkeypatch) -> None:
    """A stale timer from session 1 must not be disarmed by session 2's
    continuous worker, nor stop session 2."""
    workers: list[_RealtimeASRWorkerHandle] = []

    def _worker_start(**kwargs):  # noqa: ANN003
        worker = _continuous_worker(True)
        workers.append(worker)
        return worker

    h = _make_ptt(monkeypatch, _ConfuciusProvider(), start_worker=_worker_start)
    assert h.start() is True
    # Session 1 is continuous: its own timer is disarmed.
    h.timers.created[0].fire()
    assert h.stops["count"] == 0

    # User stops session 1 and starts session 2 (non-continuous worker).
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    workers.clear()

    def _worker_start_plain(**kwargs):  # noqa: ANN003
        worker = _continuous_worker(False)
        workers.append(worker)
        return worker

    monkeypatch.setattr("recordian.recording_controller._start_realtime_asr_worker", _worker_start_plain)
    _wait_next_start(h)
    assert len(h.timers.created) == 2

    # Session 1's stale timer fires late: identity check fails, no stop.
    h.timers.created[0].fire_force()
    assert h.stops["count"] == 1
    # Session 2's own timer is armed and stops it (worker not continuous).
    h.timers.created[1].fire()
    assert h.stops["count"] == 2


# ---------------------------------------------------------------------------
# Fatal capture callback (handle-scoped mic stop)
# ---------------------------------------------------------------------------

def test_capture_fatal_callback_stops_exact_handle(monkeypatch) -> None:
    """The worker's fatal callback stops the microphone through the exact
    record handle it was bound to; a late fire from a dead session cannot
    stop the next recording."""
    captured: list[Callable] = []

    def _worker_start(**kwargs):  # noqa: ANN003
        captured.append(kwargs["on_capture_fatal"])
        return _continuous_worker(True)

    h = _make_ptt(monkeypatch, _ConfuciusProvider(), start_worker=_worker_start)
    assert h.start() is True
    assert len(captured) == 1

    captured[0]("monitor_backlog_overflow")
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert h.stops["count"] == 1, "fatal from the live worker stops its own recording"
    assert any(
        "realtime_capture_fatal" in str(e.get("message", "")) for e in h.events
    )

    # Next session runs; the OLD callback firing late must be a no-op.
    _wait_next_start(h)
    assert h.stops["count"] == 1
    captured[0]("late_stale_fatal")
    time.sleep(0.1)
    assert h.stops["count"] == 1, "stale fatal must not stop the newer recording"
    assert h.stop() is True
    assert h.stops["count"] == 2


class _StubRefiner:
    provider_name = "stub-refiner"
    model_name = "stub-model"

    def __init__(self, **kwargs: object) -> None:
        pass


def _refine_args() -> dict[str, object]:
    return {
        "enable_text_refine": True,
        "refine_provider": "local",
        "refine_prompt": "润色",
    }


def test_segments_committed_suppresses_pipeline_refiner(monkeypatch) -> None:
    """Once prefix segments were committed through the IME token, the
    postprocess pipeline must not get the full-paragraph refiner."""
    monkeypatch.setattr("recordian.providers.Qwen3TextRefiner", _StubRefiner)

    worker_handle = _continuous_worker(True)
    worker_handle.segments_committed = 2
    worker_handle.final_text = "第一段。第二段。"
    worker_handle.outcome = "committed"
    worker_handle.commit_info = {
        "backend": "fcitx",
        "committed": True,
        "detail": "continuous_final",
        "outcome": "committed",
        "segments_committed": 2,
    }

    h = _make_ptt(
        monkeypatch,
        _ConfuciusProvider(),
        start_worker=lambda **kwargs: worker_handle,
        args_overrides=_refine_args(),
    )
    assert h.start() is True
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert len(h.contexts) == 1
    context = h.contexts[0]
    assert context.prefetched_commit_info["segments_committed"] == 2
    assert context.prefetched_commit_info["committed"] is True
    assert context.refiner is None, "full-paragraph refiner must be withheld after segment commits"


def test_no_segments_keeps_pipeline_refiner(monkeypatch) -> None:
    """A realtime worker that committed no segments keeps normal refine."""
    monkeypatch.setattr("recordian.providers.Qwen3TextRefiner", _StubRefiner)

    worker_handle = _continuous_worker(False)
    worker_handle.final_text = "短句"
    worker_handle.outcome = "committed"
    worker_handle.commit_info = {"backend": "fcitx", "committed": True, "detail": "ok"}

    h = _make_ptt(
        monkeypatch,
        _ConfuciusProvider(),
        start_worker=lambda **kwargs: worker_handle,
        args_overrides=_refine_args(),
    )
    assert h.start() is True
    assert h.stop() is True
    assert h.pipeline_done.wait(timeout=1.0) is True
    assert h.contexts[0].refiner is not None
