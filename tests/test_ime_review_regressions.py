"""Kimi K3 independent cross-review regressions for the IME lifecycle work.

Scope: contracts verified during the r4 cross-review of GLM's streaming
composition implementation (see ../../kimi-ime-review.report.md).

- ``test_commit_reply_lost_single_write`` is a PASSING contract lock:
  CommitSession applied but reply lost -> outcome "uncertain", best-effort
  CancelSession, pipeline never re-commits.
- The two former xfail locks (P1: preedit residue after an UpdatePreedit
  reply-loss; P2: empty file stream on a composition backend suppressing
  the one-shot fallback) were fixed in the r5 round and are normal passing
  regressions now — their assertions were NOT weakened.
"""
from __future__ import annotations

import argparse
import io
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from recordian import linux_commit
from recordian import postprocess_pipeline as pp
from recordian.exceptions import CommitError
from recordian.linux_dictate import RecordProcessHandle
from recordian.recording_controller import build_ptt_hotkey_handlers


class _FakeBus:
    """Addon model whose writes are applied before any reply is lost."""

    def __init__(self, *, lose_update_reply_at: int = 0, lose_commit_reply: bool = False) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.writes: list[str] = []
        self.sessions: dict[str, str] = {}
        self._updates = 0
        self._lose_update_reply_at = lose_update_reply_at
        self._lose_commit_reply = lose_commit_reply

    def __call__(self, method: str, signature: str, args: list[str]) -> str:
        self.calls.append((method, tuple(args)))
        if method == "BeginSession":
            token = f"tok-{len(self.sessions) + 1}"
            self.sessions[token] = args[0]
            return f"{token} preedit=1 frontend=gtk program=app"
        if method == "UpdatePreedit":
            self._updates += 1
            self.sessions[args[0]] = args[1]  # addon applies the write first
            if self._updates == self._lose_update_reply_at:
                raise CommitError("UpdatePreedit: timed out after 2.0s")
            return "updated"
        if method == "CommitSession":
            self.writes.append(args[1])
            self.sessions.pop(args[0], None)
            if self._lose_commit_reply:
                raise CommitError("CommitSession: timed out after 2.0s")
            return "committed gtk app"
        if method == "CancelSession":
            self.sessions.pop(args[0], None)
            return "cancelled"
        if method == "CommitText":
            self.writes.append(args[0])
            return "gtk app"
        raise AssertionError(method)

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def _pipeline_args() -> argparse.Namespace:
    return argparse.Namespace(
        config_path="",
        auto_hard_enter=False,
        debug_diagnostics=False,
        enable_streaming_refine=False,
        enable_streaming_commit=True,
        enable_remote_paste=False,
        enable_text_refine=False,
        enable_hotword_correction=False,
        enable_semif_correction=False,
        hotword=[],
        asr_context="",
    )


def _patch_pipeline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "read_wav_mono_f32", lambda p: np.array([0.3], dtype=np.float32))
    monkeypatch.setattr(pp, "send_remote_paste_from_args", lambda a, t, *, log=None: {"enabled": False})


def test_commit_reply_lost_single_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract lock: lost CommitSession reply -> exactly one write, no fallback."""
    bus = _FakeBus(lose_commit_reply=True)
    monkeypatch.setattr(linux_commit, "_fcitx_busctl_call", bus)

    committer = linux_commit.FcitxCommitter()
    session = committer.begin_composition("")
    session.update_preedit("草稿")
    result = session.commit("最终文本")
    assert result.outcome == "uncertain" and not result.committed
    assert "CancelSession" in bus.methods  # best-effort cleanup of own preedit
    assert bus.writes == ["最终文本"]

    _patch_pipeline_env(monkeypatch)

    class _Provider:
        provider_name = "stub"
        capabilities = None

        def transcribe_file(self, audio_path, hotwords=()):  # pragma: no cover
            raise AssertionError("fallback transcription must not run")

    results: list[dict] = []
    errors: list[dict] = []
    context = pp.PostprocessPipelineContext(
        args=_pipeline_args(),
        audio_path=Path("/nonexistent/voice.wav"),
        record_backend="stub",
        record_latency_ms=100.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": None},
        provider=_Provider(),
        refiner=None,
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda t: str(t).strip(),
        resolve_hotwords=lambda: [],
        on_state=lambda e: None,
        on_result=results.append,
        on_error=errors.append,
        prefetched_asr_text="最终文本",
        prefetched_commit_info={
            "backend": "fcitx",
            "committed": False,
            "detail": f"commit_failed:{result.detail}",
            "outcome": result.outcome,
        },
        prefetched_outcome=result.outcome,
        prefetched_composition_started=True,
    )
    pp.run_postprocess_pipeline(context)

    assert not errors
    assert bus.writes == ["最终文本"]  # still exactly one write
    assert bus.methods.count("CommitSession") == 1
    assert "CommitText" not in bus.methods
    assert results[0]["result"]["commit"]["committed"] is False
    assert results[0]["result"]["asr_path"] == "realtime_stale_suppressed"


def test_update_reply_loss_still_cancels_server_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 fixed: a lost UpdatePreedit reply must not strand our preedit.

    The update may have been applied before the reply was lost, so the
    session close path sends one best-effort CancelSession for the ORIGINAL
    token; the addon-side session table ends empty. The session itself is
    never reopened and a commit is never retried.
    """
    bus = _FakeBus(lose_update_reply_at=2)
    monkeypatch.setattr(linux_commit, "_fcitx_busctl_call", bus)

    session = linux_commit.FcitxCommitter().begin_composition("")
    session.update_preedit("今天天气")
    result = session.update_preedit("今天天气不错")  # applied server-side, reply lost
    assert result.outcome == "stale" and not session.active
    session.cancel()  # what the worker does on the stale path
    assert "CancelSession" in bus.methods
    assert bus.sessions == {}


def test_empty_stream_on_composition_backend_falls_back_to_oneshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2 fixed: an empty stream with zero preedit writes retries one-shot ASR.

    The retry commits through the SAME still-bound composition session, so
    focus lost during the retry refuses the commit instead of letting an
    unbound CommitText land in whatever has focus.
    """
    bus = _FakeBus()
    monkeypatch.setattr(linux_commit, "_fcitx_busctl_call", bus)
    _patch_pipeline_env(monkeypatch)

    from types import SimpleNamespace

    class _EmptyStreamProvider:
        provider_name = "stub"
        capabilities = SimpleNamespace(
            supports_hotwords=False, supports_context=False, supports_language_hint=False
        )
        transcribe_file_calls = 0

        def supports_file_streaming(self) -> bool:
            return True

        def supports_realtime_transcription(self) -> bool:
            return False

        def transcribe_file_stream(self, audio_path, hotwords=()):
            return iter([])

        def transcribe_file(self, audio_path, hotwords=()):
            self.transcribe_file_calls += 1
            return SimpleNamespace(text="完整转写结果", detected_language="zh")

    provider = _EmptyStreamProvider()
    results: list[dict] = []
    errors: list[dict] = []
    context = pp.PostprocessPipelineContext(
        args=_pipeline_args(),
        audio_path=Path("/nonexistent/voice.wav"),
        record_backend="stub",
        record_latency_ms=100.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": None},
        provider=provider,
        refiner=None,
        committer=linux_commit.FcitxCommitter(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda t: str(t).strip(),
        resolve_hotwords=lambda: [],
        on_state=lambda e: None,
        on_result=results.append,
        on_error=errors.append,
    )
    pp.run_postprocess_pipeline(context)

    assert not errors
    assert provider.transcribe_file_calls == 1
    assert results[0]["result"]["text"] == "完整转写结果"


# ---------------------------------------------------------------------------
# r5 item 5 (Kimi K3): deterministic delayed/pending-Begin vs controller
# deadline. The pre-join composition_started marker is False in both tests;
# the Begin call is genuinely in flight when the controller join times out.
# ---------------------------------------------------------------------------


def _ptt_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "cooldown_ms": 0,
        "record_backend": "ffmpeg-pulse",
        "commit_backend": "fcitx",
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
        "asr_context": "",
        "auto_hard_enter": False,
        "enable_streaming_refine": False,
        "enable_streaming_commit": True,
        "enable_remote_paste": False,
        "enable_hotword_correction": False,
        "enable_semif_correction": False,
        "config_path": "",
        "capture_refine_samples": False,
        "asr_timeout_s": 0.01,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class _FakeProcess:
    def poll(self) -> int:
        return 0


def _patch_controller_env(monkeypatch: pytest.MonkeyPatch, committer: object, provider: object) -> None:
    def _fake_start_record_process(**kwargs: object) -> RecordProcessHandle:
        output_path = kwargs["output_path"]
        output_path.write_bytes(b"")
        return RecordProcessHandle(
            process=_FakeProcess(),
            monitor_stream=io.BytesIO(b""),
            monitor_sample_rate=16000,
            monitor_channels=1,
        )

    monkeypatch.setattr("recordian.recording_controller.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.recording_controller.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.recording_controller.resolve_committer", lambda backend: committer)
    monkeypatch.setattr("recordian.recording_controller.create_provider", lambda args: provider)
    monkeypatch.setattr("recordian.recording_controller.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.recording_controller.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.recording_controller.stop_record_process", lambda *args, **kwargs: None)


class _RealtimeStubProvider:
    provider_name = "stub"
    capabilities = None

    def __init__(self) -> None:
        self.asr_cancelled = False
        self.transcribe_file_calls = 0

    def supports_realtime_transcription(self) -> bool:
        return True

    def start_realtime_session(self, hotwords=()):
        outer = self

        class _ASRSession:
            elapsed_ms = 1.0

            def push_audio(self, chunk):
                return {"text": ""}

            def finish(self):
                return SimpleNamespace(text="", detected_language="")

            def cancel(self):
                outer.asr_cancelled = True

        return _ASRSession()

    def transcribe_file(self, audio_path, hotwords=()):
        self.transcribe_file_calls += 1
        return SimpleNamespace(text="全文兜底", detected_language="zh")


def test_controller_timeout_begin_inflight_during_join_suppresses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delayed-Begin proof: pre-join composition_started is False, the real
    worker's BeginSession is IN FLIGHT when the controller deadline fires.
    The controller must still classify the outcome as uncertain (suppressed)
    — and once the late Begin returns, the cancelled worker must write zero
    preedit/commits and retract the session."""
    bus_gate = threading.Event()
    begin_started = threading.Event()

    class _BlockingBeginBus:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, method: str, signature: str, args: list[str]) -> str:
            self.calls.append(method)
            if method == "BeginSession":
                begin_started.set()
                assert bus_gate.wait(timeout=5.0), "test must release Begin"
                return "tok-delayed preedit=1 frontend=gtk program=app"
            if method == "CancelSession":
                return "cancelled"
            raise AssertionError(f"cancelled worker must never call {method}")

    bus = _BlockingBeginBus()
    monkeypatch.setattr(linux_commit, "_fcitx_busctl_call", bus)
    monkeypatch.setattr(
        "recordian.realtime_asr.open_monitor_stream_reader",
        lambda handle: io.BytesIO(b""),
    )

    provider = _RealtimeStubProvider()
    committer = linux_commit.FcitxCommitter()  # composition-capable chain
    _patch_controller_env(monkeypatch, committer, provider)

    events: list[dict[str, object]] = []
    captured: list[object] = []
    postprocess_done = threading.Event()

    def _capture_pipeline(context: object) -> None:
        captured.append(context)
        postprocess_done.set()

    monkeypatch.setattr("recordian.recording_controller.run_postprocess_pipeline", _capture_pipeline)

    start_recording, stop_recording, _, _ = build_ptt_hotkey_handlers(
        args=_ptt_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )
    assert start_recording() is True
    assert begin_started.wait(timeout=2.0), "worker must be inside BeginSession"
    # Deadline fires while Begin is in flight; pre-join marker is False.
    assert stop_recording() is True
    assert postprocess_done.wait(timeout=3.0), "controller must reach the pipeline"

    context = captured[0]
    # Marker was False both pre-join and at the post-cancel re-read (Begin
    # still blocked) — only the composition-capable classification saves us.
    assert context.prefetched_composition_started is False
    assert context.prefetched_commit_info is not None
    assert context.prefetched_commit_info["outcome"] == "uncertain"
    assert "begin_may_be_pending" in str(context.prefetched_commit_info["detail"])
    assert context.prefetched_outcome == "uncertain"

    # Now let the late Begin return: the cancelled worker binds, immediately
    # cancels, and writes nothing.
    bus_gate.set()
    deadline = threading.Event()
    worker_done = threading.Event()

    def _join_worker() -> None:
        # The controller state holds the worker handle; find it via events is
        # not exposed, so wait until the bus observed the CancelSession.
        while "CancelSession" not in bus.calls and not deadline.is_set():
            deadline.wait(0.005)
        worker_done.set()

    joiner = threading.Thread(target=_join_worker, daemon=True)
    joiner.start()
    assert worker_done.wait(timeout=3.0), "late-bound session must be retracted"
    assert bus.calls == ["BeginSession", "CancelSession"]
    assert provider.asr_cancelled is True
    assert provider.transcribe_file_calls == 0


def test_controller_timeout_legacy_backend_keeps_full_audio_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: a legacy (no composition API) committer with a slow
    realtime worker must KEEP the full-audio fallback — one plain commit of
    the one-shot transcript, nothing suppressed."""
    read_gate = threading.Event()

    class _BlockingReader:
        def read(self, n: int) -> bytes:
            read_gate.wait(timeout=5.0)
            return b""

        def close(self) -> None:
            pass

    class _LegacyCommitter:
        backend_name = "xdotool-clipboard"
        target_window_id = None

        def __init__(self) -> None:
            self.commits: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.commits.append(text)
            return SimpleNamespace(backend=self.backend_name, committed=True, detail="paste:ok")

    provider = _RealtimeStubProvider()
    committer = _LegacyCommitter()
    _patch_controller_env(monkeypatch, committer, provider)
    monkeypatch.setattr(
        "recordian.realtime_asr.open_monitor_stream_reader",
        lambda handle: _BlockingReader(),
    )
    monkeypatch.setattr(pp, "read_wav_mono_f32", lambda p: np.array([0.3], dtype=np.float32))
    monkeypatch.setattr(pp, "send_remote_paste_from_args", lambda a, t, *, log=None: {"enabled": False})

    events: list[dict[str, object]] = []
    result_seen = threading.Event()

    def _on_result(event: dict[str, object]) -> None:
        events.append(event)
        result_seen.set()

    # The worker's ASR cancel unblocks the reader so the thread can exit.
    orig_start_session = provider.start_realtime_session

    def _start_session(hotwords=()):
        session = orig_start_session(hotwords=hotwords)
        orig_cancel = session.cancel

        def _cancel() -> None:
            orig_cancel()
            read_gate.set()

        session.cancel = _cancel
        return session

    provider.start_realtime_session = _start_session  # type: ignore[method-assign]

    start_recording, stop_recording, _, _ = build_ptt_hotkey_handlers(
        args=_ptt_args(),
        on_result=_on_result,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )
    assert start_recording() is True
    assert stop_recording() is True
    assert result_seen.wait(timeout=5.0), "legacy fallback must produce a result"

    result = next(e["result"] for e in events if e.get("event") == "result")
    assert result["text"] == "全文兜底"
    assert result["commit"]["committed"] is True
    assert committer.commits == ["全文兜底"]  # exactly one full-audio commit
    assert provider.transcribe_file_calls == 1
    assert any(
        "realtime_asr_timeout_fallback" in str(e.get("message", "")) for e in events
    )
