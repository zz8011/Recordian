"""Streaming preedit composition contract tests.

Covers the focused-InputContext composition protocol end to end:
fcitx session state machine (Python side), realtime worker streaming,
cancel/finish distinctions, stale-session suppression, unicode preedit,
and the provider → worker → postprocess → committer chain.
"""
from __future__ import annotations

import argparse
import io
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from recordian.linux_commit import (
    CommitterWithFallback,
    FcitxCommitter,
    FcitxStreamingSession,
    open_composition_session,
)
from recordian.linux_dictate import RecordProcessHandle
from recordian.postprocess_pipeline import (
    PostprocessPipelineContext,
    run_postprocess_pipeline,
)
from recordian.realtime_asr import _start_realtime_asr_worker

# ---------------------------------------------------------------------------
# Fake fcitx session bus
# ---------------------------------------------------------------------------

class FakeFcitxBus:
    """In-process stand-in for the Recordian fcitx addon DBus API.

    Message styles mirror the two real transports:
    - default: addon messages embed the DBus error name token
      ("... (org.fcitx.Fcitx.Recordian.Error.StaleSession)").
    - strip_error_names=True: plain busctl stderr text without any name
      ("Call failed: unknown session") — the exact shape Kimi's native
      harness observed, which must still classify fail-closed.
    """

    def __init__(self, *, preedit_capable: bool = True) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.sessions: dict[str, dict[str, object]] = {}
        self.preedit_capable = preedit_capable
        self.begin_fails = False
        # token -> True once the context "lost focus" (StaleSession errors)
        self.stale_tokens: set[str] = set()
        # method name -> error message raised instead of a reply
        self.fail_with: dict[str, str] = {}
        # busctl-style stderr without the structured DBus error name
        self.strip_error_names = False
        # CommitSession applies the write, then the reply is lost
        # (timeout after the commit reached the app).
        self.commit_reply_lost = False
        self.applied_commits: list[str] = []

    def lose_focus(self) -> None:
        self.stale_tokens.update(self.sessions.keys())

    def preedit_of(self, token: str) -> str:
        entry = self.sessions.get(token)
        return str(entry["preedit"]) if entry else ""

    def commit_texts(self) -> list[str]:
        return [args[1] for method, args in self.calls if method == "CommitSession"]

    def _error(self, method: str, name: str, message: str) -> Exception:
        from recordian.exceptions import CommitError

        if self.strip_error_names:
            # What busctl actually prints: the error NAME is gone.
            return CommitError(f"{method}: Call failed: {message}")
        return CommitError(
            f"{method}: Call failed: {message} (org.fcitx.Fcitx.Recordian.Error.{name})"
        )

    # -- busctl replacement --------------------------------------------------

    def call(self, method: str, signature: str, args: list[str]) -> str:
        self.calls.append((method, list(args)))
        if method in self.fail_with:
            from recordian.exceptions import CommitError

            raise CommitError(self.fail_with[method])
        if method == "Ping":
            return "ok"
        if method == "CommitText":
            return "committed wayland Fake App"
        if method == "BeginSession":
            if self.begin_fails:
                raise self._error(
                    "BeginSession",
                    "ExistingPreedit",
                    "input context already has a non-empty preedit",
                )
            token = f"tok{len(self.sessions) + 1}"
            self.sessions[token] = {"alive": True, "preedit": args[0]}
            return (
                f"{token} preedit={1 if self.preedit_capable else 0} "
                "frontend=wayland program=Fake App"
            )
        if method in ("UpdatePreedit", "CommitSession", "CancelSession"):
            token = args[0]
            entry = self.sessions.get(token)
            if entry is None or not entry["alive"] or token in self.stale_tokens:
                raise self._error(method, "StaleSession", "unknown session")
            if method == "UpdatePreedit":
                if not self.preedit_capable:
                    return "noop_preview_only"
                entry["preedit"] = args[1]
                return "updated"
            if method == "CommitSession":
                entry["alive"] = False
                self.applied_commits.append(args[1])
                if self.commit_reply_lost:
                    # The write reached the app; only the reply is gone.
                    from recordian.exceptions import CommitError

                    raise CommitError("CommitSession: timed out after 2.0s")
                return "committed wayland Fake App" if args[1] else "cleared"
            entry["alive"] = False
            return "cancelled"
        raise Exception(f"unexpected method {method}")


def _install_fake_bus(monkeypatch, bus: FakeFcitxBus) -> None:
    from recordian.linux_commit import which as _real_which

    monkeypatch.setattr(
        "recordian.linux_commit._fcitx_busctl_call",
        lambda method, signature, args: bus.call(method, signature, list(args)),
    )
    # Keep tests hermetic: never reach a real busctl/fcitx instance.
    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/busctl" if name == "busctl" else _real_which(name),
    )


def _record_handle(payload_len: int = 3) -> RecordProcessHandle:
    return RecordProcessHandle(
        process=SimpleNamespace(),
        monitor_stream=io.BytesIO(b"\x00\x00\x00\x00" * payload_len),
        monitor_sample_rate=4,
        monitor_channels=1,
    )


def _worker_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "enable_streaming_commit": True,
        "sample_rate": 4,
        "channels": 1,
        "debug_diagnostics": False,
        "hotword": [],
        "asr_context": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class _ScriptedSession:
    """Realtime ASR session stub with scripted partial responses."""

    elapsed_ms = 42.0

    def __init__(self, responses: list[str], *, finish_text: str = "") -> None:
        self._responses = list(responses)
        self.finish_text = finish_text
        self.finish_called = False
        self.cancel_called = False
        self.on_push: list[object] = []

    def push_audio(self, payload: bytes) -> dict[str, object]:
        for hook in self.on_push:
            if callable(hook):
                hook()
        return {"text": self._responses.pop(0) if self._responses else ""}

    def finish(self):
        self.finish_called = True
        return SimpleNamespace(text=self.finish_text, detected_language="zh")

    def cancel(self) -> None:
        self.cancel_called = True


class _RealtimeProvider:
    realtime_chunk_size_sec = 0.25

    def __init__(self, session: _ScriptedSession) -> None:
        self._session = session
        self.transcribe_file_calls = 0

    def supports_realtime_transcription(self) -> bool:
        return True

    def start_realtime_session(self, *, hotwords: list[str]):
        return self._session

    def transcribe_file(self, audio_path: Path, hotwords: list[str]):
        self.transcribe_file_calls += 1
        return SimpleNamespace(text="全文兜底", detected_language="zh")


# ---------------------------------------------------------------------------
# FcitxCommitter composition session (Python side)
# ---------------------------------------------------------------------------

def test_fcitx_begin_composition_parses_descriptor(monkeypatch) -> None:
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    committer = FcitxCommitter()
    session = committer.begin_composition("")
    assert session.token == "tok1"
    assert session.preedit_capable is True
    assert session.active
    assert "program=Fake App" in session.info


def test_fcitx_session_duplicate_commit_is_rejected_once_only(monkeypatch) -> None:
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    session = FcitxCommitter().begin_composition("")
    assert session.update_preedit("预编辑").committed
    first = session.commit("最终文本")
    assert first.committed and first.detail.startswith("committed")
    second = session.commit("最终文本")
    assert not second.committed
    assert "stale" in second.detail.lower()
    assert bus.commit_texts() == ["最终文本"]
    assert not session.active


def test_fcitx_session_update_failure_does_not_advance_state(monkeypatch) -> None:
    """ANY UpdatePreedit failure invalidates the session (fail closed).

    Real busctl strips the DBus error name from stderr, so a "transient"
    looking message cannot be told apart from a StaleSession refusal. The
    liveness of the bound context is unproven → session closed, commit
    refused, no text written.
    """
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    session = FcitxCommitter().begin_composition("")
    bus.fail_with["UpdatePreedit"] = "UpdatePreedit: broken pipe"
    result = session.update_preedit("还是预编辑")
    assert not result.committed
    assert result.outcome == "stale"
    assert not session.active
    rejected = session.commit("最终")
    assert not rejected.committed
    assert rejected.outcome == "stale"
    assert bus.commit_texts() == []  # nothing ever reached CommitSession
    assert bus.preedit_of(session.token) == ""  # UpdatePreedit never applied


def test_fcitx_session_update_failure_without_error_name_is_stale(monkeypatch) -> None:
    """Regression (native s4b): busctl prints no error name at all.

    "Call failed: unknown session" must still classify as stale and close
    the session — the old code only matched the literal "StaleSession".
    """
    bus = FakeFcitxBus()
    bus.strip_error_names = True  # exactly what busctl stderr looks like
    _install_fake_bus(monkeypatch, bus)
    session = FcitxCommitter().begin_composition("")
    bus.lose_focus()
    result = session.update_preedit("预编辑")
    assert not result.committed
    assert result.outcome == "stale"
    assert "unknown session" in result.detail
    assert not session.active
    # And the stale session can never commit afterwards.
    assert session.commit("最终").outcome == "stale"


def test_fcitx_session_stale_error_name_is_parsed_when_present(monkeypatch) -> None:
    """With the addon's embedded error-name protocol the classification is
    structured, not text-guessed (also works for gdbus-style names)."""
    from recordian.linux_commit import _parse_dbus_error_name

    assert _parse_dbus_error_name(
        "UpdatePreedit: Call failed: unknown session "
        "(org.fcitx.Fcitx.Recordian.Error.StaleSession)"
    ) == "StaleSession"
    assert (
        _parse_dbus_error_name(
            "GDBus.Error:org.fcitx.Fcitx.Recordian.Error.NoInputContext: no focus"
        )
        == "NoInputContext"
    )
    assert _parse_dbus_error_name("CommitSession: timed out after 2.0s") == ""
    # Commit with an embedded StaleSession name → stale (nothing was written).
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    session = FcitxCommitter().begin_composition("")
    bus.lose_focus()
    result = session.commit("文本")
    assert not result.committed
    assert result.outcome == "stale"
    assert bus.applied_commits == []


def test_open_composition_session_unwraps_fallback_wrapper(monkeypatch) -> None:
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    wrapped = CommitterWithFallback(
        committers=[(FcitxCommitter(), "fcitx"), (StdoutLike(), "stdout")],
        notify_on_fallback=False,
    )
    session = open_composition_session(wrapped)
    assert isinstance(session, FcitxStreamingSession)
    assert session.token == "tok1"


class StdoutLike:
    backend_name = "stdout"

    def commit(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(backend="stdout", committed=False, detail="printed")


def test_open_composition_session_returns_none_for_legacy_backend() -> None:
    assert open_composition_session(StdoutLike()) is None


# ---------------------------------------------------------------------------
# Realtime worker with composition
# ---------------------------------------------------------------------------

def _run_worker(monkeypatch, bus, provider, *, refine_enabled: bool = False, **worker_kwargs):
    events: list[dict[str, object]] = []
    _install_fake_bus(monkeypatch, bus)
    defaults: dict[str, object] = {
        "args": _worker_args(),
        "provider": provider,
        "record_handle": _record_handle(),
        "committer": FcitxCommitter(),
        "enable_local_commit": True,
        "auto_hard_enter": False,
        "resolve_hotwords": lambda: [],
        "normalize_final_text": lambda text: str(text).strip(),
        "on_state": events.append,
        "refine_enabled": refine_enabled,
    }
    defaults.update(worker_kwargs)
    worker = _start_realtime_asr_worker(**defaults)
    assert worker is not None
    worker.thread.join(timeout=2.0)
    assert not worker.thread.is_alive()
    return worker, events


def test_worker_streams_preedit_and_commits_once(monkeypatch) -> None:
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(
        _ScriptedSession(["你好", "你好世界"], finish_text="你好世界。")
    )
    worker, events = _run_worker(monkeypatch, bus, provider)

    assert worker.final_text == "你好世界。"
    assert worker.error == ""
    assert worker.commit_info == {
        "backend": "fcitx",
        "committed": True,
        "detail": "committed wayland Fake App",
        "outcome": "committed",
    }
    methods = [method for method, _ in bus.calls]
    assert methods.count("CommitSession") == 1
    assert bus.commit_texts() == ["你好世界。"]
    updates = [args[1] for method, args in bus.calls if method == "UpdatePreedit"]
    assert updates == ["你好", "你好世界", "你好世界。"]  # last one: final-text liveness probe
    partials = [e.get("text") for e in events if e.get("event") == "realtime_asr_partial"]
    assert partials == ["你好", "你好世界"]


def test_worker_revision_of_first_chars_needs_no_backspace(monkeypatch) -> None:
    """Partial revises its first characters → preedit replaced, single commit."""
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(
        _ScriptedSession(["你好世界", "你们好世界"], finish_text="你们好世界！")
    )
    worker, _events = _run_worker(monkeypatch, bus, provider)

    assert worker.commit_info is not None and worker.commit_info["committed"]
    updates = [args[1] for method, args in bus.calls if method == "UpdatePreedit"]
    assert updates == ["你好世界", "你们好世界", "你们好世界！"]
    # Structural counter-proof: the composition protocol has no delete /
    # backspace method at all — the fake bus would raise on any.
    assert bus.commit_texts() == ["你们好世界！"]
    assert all("BackSpace" not in str(args) for _, args in bus.calls)


def test_worker_focus_lost_mid_stream_suppresses_everything(monkeypatch) -> None:
    bus = FakeFcitxBus()
    session = _ScriptedSession(["你好", "你好吗"], finish_text="你好吗。")

    def _lose_focus_on_second_push() -> None:
        if session.finish_called or len(bus.calls) >= 3:
            bus.lose_focus()

    session.on_push.append(_lose_focus_on_second_push)
    provider = _RealtimeProvider(session)
    worker, events = _run_worker(monkeypatch, bus, provider)

    assert worker.session_stale is True
    assert worker.final_text == ""
    assert worker.commit_info == {
        "backend": "fcitx",
        "committed": False,
        "detail": "realtime_session_stale",
        "outcome": "stale",
    }
    methods = [method for method, _ in bus.calls]
    assert "CommitSession" not in methods
    assert "CommitText" not in methods
    assert any("realtime_session_stale" in str(e.get("message", "")) for e in events)


def test_worker_cancel_then_late_finish_never_commits(monkeypatch) -> None:
    bus = FakeFcitxBus()
    session = _ScriptedSession(["部分", "更多部分"], finish_text="迟到的完整结果")

    # Gate the first push until the test thread has registered the worker
    # handle: otherwise the worker can drain the whole fake stream before
    # the "controller" timeout has anything to cancel (startup race).
    push_gate = threading.Event()

    def _cancel_from_controller() -> None:
        push_gate.wait(timeout=2.0)
        worker_handle = _cancel_state.get("worker")
        if worker_handle is not None:
            worker_handle.cancel_event.set()

    _cancel_state: dict[str, object] = {"worker": None}
    session.on_push.append(_cancel_from_controller)
    provider = _RealtimeProvider(session)

    events: list[dict[str, object]] = []
    _install_fake_bus(monkeypatch, bus)
    worker = _start_realtime_asr_worker(
        args=_worker_args(),
        provider=provider,
        record_handle=_record_handle(),
        committer=FcitxCommitter(),
        enable_local_commit=True,
        auto_hard_enter=False,
        resolve_hotwords=lambda: [],
        normalize_final_text=lambda text: str(text).strip(),
        on_state=events.append,
    )
    _cancel_state["worker"] = worker
    push_gate.set()
    worker.thread.join(timeout=2.0)

    # Late finish must not run and nothing may be committed.
    assert session.finish_called is False
    assert session.cancel_called is True
    assert worker.final_text == ""
    assert worker.commit_info == {
        "backend": "fcitx",
        "committed": False,
        "detail": "realtime_cancelled",
        "outcome": "cancelled",
    }
    methods = [method for method, _ in bus.calls]
    assert "CommitSession" not in methods
    # The partial was preserved for diagnostics but never committed.
    assert worker.partial_text == "部分"
    assert "CancelSession" in methods


def test_worker_provider_failure_cancels_and_never_commits_partial(monkeypatch) -> None:
    bus = FakeFcitxBus()

    class _FailingSession(_ScriptedSession):
        def push_audio(self, payload: bytes) -> dict[str, object]:
            raise RuntimeError("asr connection dropped")

    provider = _RealtimeProvider(_FailingSession([], finish_text="不该出现"))
    worker, _events = _run_worker(monkeypatch, bus, provider)

    assert worker.error.startswith("RuntimeError")
    # The partial hypothesis is never silently treated as the final text.
    assert worker.final_text == ""
    assert worker.commit_info is not None
    assert worker.commit_info["committed"] is False
    assert "realtime_failed" in str(worker.commit_info["detail"])
    methods = [method for method, _ in bus.calls]
    assert "CommitSession" not in methods
    assert "CancelSession" in methods


def test_worker_release_preedit_when_refine_enabled(monkeypatch) -> None:
    """r4 refine contract: the session STAYS BOUND for the pipeline.

    The worker no longer cancels the session before refine — cancelling
    would orphan the InputContext binding and force the final commit into
    whatever has focus later. The live session travels on the worker
    handle; the pipeline commits through the same token exactly once.
    """
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(_ScriptedSession(["草稿"], finish_text="最终草稿"))
    worker, _events = _run_worker(monkeypatch, bus, provider, refine_enabled=True)

    methods = [method for method, _ in bus.calls]
    assert "CommitSession" not in methods
    assert "CancelSession" not in methods  # early cancel is the old contract
    assert worker.final_text == "最终草稿"
    assert worker.commit_info == {
        "backend": "fcitx",
        "committed": False,
        "detail": "realtime_preedit_released_for_refine",
        "outcome": "released_for_refine",
    }
    # The live session is carried for the pipeline, still bound to the
    # original input context, with our preedit intact.
    carried = worker.composition_session
    assert carried is not None and carried.active
    assert carried.token in bus.sessions
    assert bus.preedit_of(carried.token) == "草稿"


def test_worker_unicode_emoji_preedit_roundtrip(monkeypatch) -> None:
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(
        _ScriptedSession(["📝草稿👶", "📝草稿👶🌈"], finish_text="📝定稿👶🌈")
    )
    worker, _events = _run_worker(monkeypatch, bus, provider)

    updates = [args[1] for method, args in bus.calls if method == "UpdatePreedit"]
    assert updates == ["📝草稿👶", "📝草稿👶🌈", "📝定稿👶🌈"]
    assert bus.commit_texts() == ["📝定稿👶🌈"]
    assert worker.final_text == "📝定稿👶🌈"


# ---------------------------------------------------------------------------
# Postprocess pipeline integration
# ---------------------------------------------------------------------------

def _pipeline_context(
    *,
    provider,
    committer,
    args: argparse.Namespace,
    prefetched_asr_text: str = "",
    prefetched_commit_info: dict | None = None,
    prefetched_transcribe_latency_ms: float = 0.0,
    prefetched_outcome: str = "",
    prefetched_composition_started: bool = False,
    composition_session=None,
    prefetched_semif_applied: bool = False,
    refiner=None,
    auto_lexicon=None,
    state_events: list | None = None,
    result_events: list | None = None,
    error_events: list | None = None,
) -> PostprocessPipelineContext:
    state_events = state_events if state_events is not None else []
    result_events = result_events if result_events is not None else []
    error_events = error_events if error_events is not None else []
    return PostprocessPipelineContext(
        args=args,
        audio_path=Path("/nonexistent/voice.wav"),
        record_backend="ffmpeg-pulse",
        record_latency_ms=100.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=provider,
        refiner=refiner,
        committer=committer,
        auto_lexicon=auto_lexicon,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
        prefetched_asr_text=prefetched_asr_text,
        prefetched_commit_info=prefetched_commit_info,
        prefetched_transcribe_latency_ms=prefetched_transcribe_latency_ms,
        prefetched_outcome=prefetched_outcome,
        prefetched_composition_started=prefetched_composition_started,
        composition_session=composition_session,
        prefetched_semif_applied=prefetched_semif_applied,
    )


def _pipeline_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config_path": "",
        "auto_hard_enter": False,
        "debug_diagnostics": False,
        "enable_streaming_refine": False,
        "enable_streaming_commit": True,
        "enable_remote_paste": False,
        "enable_text_refine": False,
        "hotword": [],
        "asr_context": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _patch_pipeline_env(monkeypatch) -> None:
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )


def test_chain_provider_worker_postprocess_committer_single_commit(monkeypatch) -> None:
    """Full chain: streaming worker commits once; pipeline never re-commits."""
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(
        _ScriptedSession(["克劳德", "克劳德很强"], finish_text="克劳德很强")
    )
    worker, _events = _run_worker(
        monkeypatch,
        bus,
        provider,
        args=_worker_args(asr_context="克劳德→Claude"),
        resolve_hotwords=lambda: ["Claude"],
    )
    # Hotword correction shaped both the preedit preview and the final commit.
    updates = [args[1] for method, args in bus.calls if method == "UpdatePreedit"]
    assert updates == ["Claude", "Claude很强", "Claude很强"]
    assert bus.commit_texts() == ["Claude很强"]
    assert worker.commit_info is not None and worker.commit_info["committed"]

    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(hotword=[], asr_context="克劳德→Claude"),
        prefetched_asr_text=worker.final_text,
        prefetched_commit_info=worker.commit_info,
        prefetched_transcribe_latency_ms=worker.transcribe_latency_ms,
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # Exactly one commit for the whole utterance: the composition commit.
    assert bus.commit_texts() == ["Claude很强"]
    methods = [method for method, _ in bus.calls]
    assert "CommitText" not in methods
    assert provider.transcribe_file_calls == 0
    result = result_events[0]["result"]
    assert result["text"] == "Claude很强"
    assert result["commit"]["committed"] is True
    assert result["asr_path"] == "prefetched"


def test_pipeline_suppresses_commit_after_focus_lost_stale_result(monkeypatch) -> None:
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(_ScriptedSession(["部分"], finish_text="完整"))
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="",
        prefetched_commit_info={
            "backend": "fcitx",
            "committed": False,
            "detail": "realtime_session_stale",
        },
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # Neither fallback transcription nor a fallback commit may run.
    assert provider.transcribe_file_calls == 0
    methods = [method for method, _ in bus.calls]
    assert methods == []  # not even a composition session is started
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["asr_path"] == "realtime_stale_suppressed"
    assert any("realtime_commit_suppressed" in str(e.get("message", "")) for e in state_events)


def test_pipeline_asr_failure_falls_back_to_single_full_commit(monkeypatch) -> None:
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(_ScriptedSession([], finish_text="不该提交"))

    class _FullAudioProvider(_RealtimeProvider):
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):
            self.transcribe_file_calls += 1
            return SimpleNamespace(text="完整音频转写", detected_language="zh")

    provider = _FullAudioProvider(_ScriptedSession([], finish_text="x"))
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="",
        prefetched_commit_info={
            "backend": "fcitx",
            "committed": False,
            "detail": "realtime_failed:RuntimeError: asr connection dropped",
        },
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # Full-audio fallback runs exactly once and commits exactly once.
    assert provider.transcribe_file_calls == 1
    texts = [args[0] for method, args in bus.calls if method == "CommitText"]
    assert texts == ["完整音频转写"]
    methods = [method for method, _ in bus.calls]
    assert "CommitSession" not in methods
    result = result_events[0]["result"]
    assert result["text"] == "完整音频转写"
    assert result["commit"]["committed"] is True


def test_pipeline_refine_differs_from_partial_and_commits_once(monkeypatch) -> None:
    bus = FakeFcitxBus()

    class _Refiner:
        prompt_template = "请整理：{text}"

        def refine(self, text: str) -> str:
            return "精炼后的最终文本"

    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=_RealtimeProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="实时部分文本",
        prefetched_commit_info={
            "backend": "fcitx",
            "committed": False,
            "detail": "realtime_preedit_released_for_refine",
        },
        refiner=_Refiner(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # Final refine output (≠ partial) is committed exactly once.
    texts = [args[0] for method, args in bus.calls if method == "CommitText"]
    assert texts == ["精炼后的最终文本"]
    result = result_events[0]["result"]
    assert result["text"] == "精炼后的最终文本"
    assert result["commit"]["committed"] is True


def test_pipeline_timeout_suppressed_never_falls_back(monkeypatch) -> None:
    """Controller deadline hit with a live composition session: the outcome
    is uncertain, so neither a full transcription nor any commit may run."""
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(_ScriptedSession([], finish_text="不该出现"))
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="",
        prefetched_commit_info={
            "backend": "fcitx",
            "committed": False,
            "detail": "realtime_asr_timeout_suppressed",
        },
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # No fallback transcription, no commit of any kind.
    assert provider.transcribe_file_calls == 0
    assert bus.calls == []
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["asr_path"] == "realtime_stale_suppressed"
    assert any("realtime_commit_suppressed" in str(e.get("message", "")) for e in state_events)


class _RecordingLexicon:
    """auto_lexicon stand-in accepting both observe_accepted signatures."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def observe_accepted(self, text: str, source: str | None = None):
        self.calls.append((text, source))
        return 0


def test_pipeline_labels_auto_lexicon_source(monkeypatch) -> None:
    """observe_accepted is called with asr/corrected/refined labels —
    never user_confirmed, which only a real user confirmation may set."""
    bus = FakeFcitxBus()
    lexicon = _RecordingLexicon()
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=_RealtimeProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="原始转写",
        prefetched_commit_info=None,
        auto_lexicon=lexicon,
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # ASR text committed unchanged -> source="asr".
    assert lexicon.calls == [("原始转写", "asr")]

    class _UpperRefiner:
        prompt_template = "请整理：{text}"

        def refine(self, text: str) -> str:
            return "精炼输出"

    lexicon2 = _RecordingLexicon()
    context2 = _pipeline_context(
        provider=_RealtimeProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="原始转写",
        prefetched_commit_info=None,
        auto_lexicon=lexicon2,
        refiner=_UpperRefiner(),
        state_events=[],
        result_events=result_events[:0] or [],
        error_events=[],
    )
    run_postprocess_pipeline(context2)
    # Refiner changed the text -> source="refined".
    assert lexicon2.calls == [("精炼输出", "refined")]


def test_pipeline_auto_lexicon_incompatible_signature_fails_closed(monkeypatch) -> None:
    """An auto_lexicon collaborator that cannot take the provenance keyword
    is INCOMPATIBLE, not tolerated: exactly one call is attempted (with
    source), its TypeError never triggers a second source-less call — the
    legacy unlabeled API increments accept_count, the same counter that
    feeds the auto hotword pool — and the pipeline still finishes the
    utterance without learning anything."""

    class _LegacyLexicon:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def observe_accepted(self, text: str, source: str | None = None):
            if source is not None:
                self.calls.append((text, source))
                raise TypeError("observe_accepted() got an unexpected keyword argument 'source'")
            self.calls.append((text, None))
            return 3

    bus = FakeFcitxBus()
    lexicon = _LegacyLexicon()
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=_RealtimeProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="原文",
        prefetched_commit_info=None,
        auto_lexicon=lexicon,
        state_events=[],
        result_events=[],
        error_events=[],
    )
    run_postprocess_pipeline(context)
    # One labeled attempt, TypeError, no source-less retry, no learning.
    assert lexicon.calls == [("原文", "asr")]
    # The utterance itself is unaffected (committed once, text preserved).
    texts = [args[0] for method, args in bus.calls if method == "CommitText"]
    assert texts == ["原文"]


def test_worker_uses_streaming_corrector_for_preedit_and_final(monkeypatch) -> None:
    """The bounded StreamingHotwordCorrector drives preedit + final commit.

    A spy replaces the real class: submit marks "S:", poll "P:", finish
    "F:". The worker must feed raw ASR text in, display the returned
    snapshots, and commit the finish() result exactly once.
    """
    import recordian.streaming_correction as sc

    constructed: dict[str, object] = {}

    class _SpyCorrector:
        def __init__(self, hotwords, *, endpoint="", timeout_s=0.12, enabled=False, **_):
            constructed["hotwords"] = list(hotwords)
            constructed["endpoint"] = endpoint
            constructed["timeout_s"] = timeout_s
            constructed["enabled"] = enabled
            self.cancelled = 0

        def submit(self, text):
            return f"S:{text}"

        def poll(self, text):
            return f"P:{text}"

        def finish(self, text):
            return f"F:{text}"

        def cancel(self):
            self.cancelled += 1

        def close(self):
            pass

    monkeypatch.setattr(sc, "StreamingHotwordCorrector", _SpyCorrector)
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(
        _ScriptedSession(["你好", "你好世界"], finish_text="你好世界。")
    )
    worker, events = _run_worker(
        monkeypatch,
        bus,
        provider,
        args=_worker_args(
            enable_semif_correction=True,
            semif_endpoint="http://127.0.0.1:8080/judge",
            semif_timeout_s=0.2,
        ),
    )

    # Constructor received Kimi's args and the merged hotword lexicon.
    assert constructed["endpoint"] == "http://127.0.0.1:8080/judge"
    assert constructed["timeout_s"] == 0.2
    assert constructed["enabled"] is True
    # Preedit showed the submit() snapshots; the final commit used finish().
    updates = [args[1] for method, args in bus.calls if method == "UpdatePreedit"]
    assert updates[:2] == ["S:你好", "S:你好世界"]
    assert bus.commit_texts() == ["F:你好世界。"]
    assert worker.final_text == "F:你好世界。"
    assert worker.commit_info is not None and worker.commit_info["committed"]


# ---------------------------------------------------------------------------
# r4 counter-examples: refused begin, lost commit reply, refine session
# ---------------------------------------------------------------------------

def test_worker_begin_refused_asr_succeeds_zero_writes(monkeypatch) -> None:
    """Real-chain counter-example: Begin refused, ASR succeeds → zero writes.

    The user has their own composition (Rime preedit) / no focus / a
    sensitive field, so the addon refuses BeginSession. The catch-all must
    NOT treat this as "no composition capability": no CommitText, no
    backspace, no clipboard — only the transcript for manual copy.
    """
    bus = FakeFcitxBus()
    bus.begin_fails = True
    bus.strip_error_names = True  # plain busctl stderr, no error name
    provider = _RealtimeProvider(
        _ScriptedSession(["你好", "你好世界"], finish_text="你好世界。")
    )
    worker, events = _run_worker(monkeypatch, bus, provider)

    # ASR ran to completion and produced a transcript...
    assert worker.final_text == "你好世界。"
    assert worker.error == ""
    # ...but the outcome is terminal "suppressed", never "no_composition".
    assert worker.composition_refused is True
    assert worker.composition_started is False
    assert worker.outcome == "suppressed"
    assert worker.commit_info is not None
    assert worker.commit_info["outcome"] == "suppressed"
    assert "refused" in str(worker.commit_info["detail"])
    # Zero writes of any kind: no preedit, no commit, no cancel, no legacy
    # CommitText, nothing that could touch the user's preedit or a new focus.
    methods = [method for method, _ in bus.calls]
    assert methods == ["BeginSession"]
    assert any(
        "realtime_composition_refused" in str(e.get("message", "")) for e in events
    )


def test_pipeline_begin_refused_worker_suppresses_all_fallback(monkeypatch) -> None:
    """Controller→pipeline continuation: refused begin must not leak into a
    plain CommitText / full-audio fallback commit."""
    bus = FakeFcitxBus()
    bus.begin_fails = True
    provider = _RealtimeProvider(_ScriptedSession(["部分"], finish_text="完整文本"))

    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text="完整文本",
        prefetched_commit_info={
            "backend": "fcitx",
            "committed": False,
            "detail": "realtime_composition_refused: user preedit",
            "outcome": "suppressed",
        },
        prefetched_outcome="suppressed",
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # No full-audio transcription, no commit of ANY kind.
    assert provider.transcribe_file_calls == 0
    assert bus.calls == []
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "suppressed"
    assert result["asr_path"] == "realtime_stale_suppressed"
    # The transcript is preserved for manual copy.
    assert result["text"] == "完整文本"
    assert any(
        "realtime_commit_suppressed" in str(e.get("message", "")) for e in state_events
    )


def test_pipeline_file_stream_begin_refused_zero_commits(monkeypatch) -> None:
    """File-streaming path: a refused composition session must suppress the
    whole pass — no streaming writes AND no plain one-shot CommitText."""

    class _FileStreamProvider(_RealtimeProvider):
        def supports_file_streaming(self) -> bool:
            return True

        def transcribe_file_stream(self, audio_path, hotwords):
            yield "流式"
            yield "流式文本"

    bus = FakeFcitxBus()
    bus.begin_fails = True
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=_FileStreamProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    methods = [method for method, _ in bus.calls]
    # Begin was attempted and refused; nothing else may be written —
    # in particular no CommitText fallback that would land in whatever
    # context currently has focus.
    assert methods == ["BeginSession"]
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "suppressed"
    assert result["asr_path"] == "stream_composition_refused_suppressed"
    assert any(
        "stream_composition_refused" in str(e.get("message", "")) for e in state_events
    )


def test_chain_commit_written_then_reply_lost_never_recommits(monkeypatch) -> None:
    """End-to-end: CommitSession reached the app, the reply was lost.

    The write may already be in the input field — committing the same text
    again through ANY path (second CommitSession, CommitText, full-audio
    fallback) would duplicate it. Outcome stays "uncertain", exactly one
    write ever reaches the bus, and the pipeline suppresses everything.
    """
    bus = FakeFcitxBus()
    bus.commit_reply_lost = True
    provider = _RealtimeProvider(
        _ScriptedSession(["你好"], finish_text="你好世界。")
    )
    worker, _events = _run_worker(monkeypatch, bus, provider)

    assert worker.final_text == "你好世界。"
    assert worker.commit_info is not None
    assert worker.commit_info["committed"] is False
    assert worker.commit_info["outcome"] == "uncertain"
    # The one applied write plus the cleanup cancel; the commit itself is
    # never retried through the session...
    assert bus.applied_commits == ["你好世界。"]
    assert worker.session_stale is False  # unknown ≠ provably-stale

    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text=worker.final_text,
        prefetched_commit_info=worker.commit_info,
        prefetched_transcribe_latency_ms=worker.transcribe_latency_ms,
        prefetched_outcome=worker.outcome,
        prefetched_composition_started=worker.composition_started,
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # ...and never through a fallback either.
    assert provider.transcribe_file_calls == 0
    methods = [method for method, _ in bus.calls]
    assert methods.count("CommitSession") == 1  # the single lost-reply attempt
    assert "CommitText" not in methods
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "uncertain"
    assert result["asr_path"] == "realtime_stale_suppressed"


def test_refine_session_commit_through_same_token_once(monkeypatch) -> None:
    """Refine happy path: the worker-carried session commits exactly once,
    through the SAME token (same input context), after refinement."""
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(_ScriptedSession(["草稿"], finish_text="最终草稿"))
    worker, _events = _run_worker(monkeypatch, bus, provider, refine_enabled=True)
    carried = worker.composition_session
    assert carried is not None and carried.active

    class _Refiner:
        prompt_template = "请整理：{text}"

        def refine(self, text: str) -> str:
            return "整理后的最终草稿"

    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text=worker.final_text,
        prefetched_commit_info=worker.commit_info,
        prefetched_transcribe_latency_ms=worker.transcribe_latency_ms,
        prefetched_outcome=worker.outcome,
        composition_session=carried,
        prefetched_semif_applied=True,
        refiner=_Refiner(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # One commit through the carried token, refined text, same context.
    commits = [(args[0], args[1]) for method, args in bus.calls if method == "CommitSession"]
    assert commits == [(carried.token, "整理后的最终草稿")]
    methods = [method for method, _ in bus.calls]
    assert "CommitText" not in methods
    assert provider.transcribe_file_calls == 0
    result = result_events[0]["result"]
    assert result["text"] == "整理后的最终草稿"
    assert result["commit"]["committed"] is True
    assert result["asr_path"] == "realtime_refine_session_commit"


def test_refine_session_focus_lost_during_refine_rejects_commit(monkeypatch) -> None:
    """Refine counter-example: the user clicks away / resets / types while
    the refiner runs. The carried session goes stale; the commit through the
    original token FAILS and no fallback may write into the new focus."""
    bus = FakeFcitxBus()
    provider = _RealtimeProvider(_ScriptedSession(["草稿"], finish_text="最终草稿"))
    worker, _events = _run_worker(monkeypatch, bus, provider, refine_enabled=True)
    carried = worker.composition_session
    assert carried is not None and carried.active

    class _Refiner:
        prompt_template = "请整理：{text}"

        def refine(self, text: str) -> str:
            # The user moved focus / reset / typed while we were refining.
            bus.lose_focus()
            return "整理后的最终草稿"

    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        prefetched_asr_text=worker.final_text,
        prefetched_commit_info=worker.commit_info,
        prefetched_transcribe_latency_ms=worker.transcribe_latency_ms,
        prefetched_outcome=worker.outcome,
        composition_session=carried,
        prefetched_semif_applied=True,
        refiner=_Refiner(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # The stale session refused the commit; nothing was applied.
    assert bus.applied_commits == []
    methods = [method for method, _ in bus.calls]
    assert "CommitSession" in methods  # attempted once through the old token
    assert "CommitText" not in methods  # never into the new focus
    assert provider.transcribe_file_calls == 0
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "stale"
    # Transcript preserved for manual copy.
    assert result["text"] == "整理后的最终草稿"
    assert any(
        "refine_session_commit_failed" in str(e.get("message", "")) for e in state_events
    )


def test_pipeline_refine_stream_begin_refused_zero_commits(monkeypatch) -> None:
    """Refine-streaming path: refused composition must suppress the pass —
    the refiner may run, but no preedit, no CommitText, no fallback."""

    class _FileStreamProvider(_RealtimeProvider):
        def supports_file_streaming(self) -> bool:
            return False  # keep the ASR side oneshot; refine stream is the target

    class _RefineStreamRefiner:
        prompt_template = "请整理：{text}"
        refine_calls: list[str] = []

        def refine_stream(self, text: str):
            self.refine_calls.append(text)
            yield "整理结果"

    bus = FakeFcitxBus()
    bus.begin_fails = True
    refiner = _RefineStreamRefiner()
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)

    class _OneshotProvider(_FileStreamProvider):
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):
            self.transcribe_file_calls += 1
            return SimpleNamespace(text="原始转写", detected_language="zh")

    context = _pipeline_context(
        provider=_OneshotProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        refiner=refiner,
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    methods = [method for method, _ in bus.calls]
    assert methods == ["BeginSession"]  # refused; nothing else written
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "suppressed"
    assert any(
        "stream_composition_refused" in str(e.get("message", "")) for e in state_events
    )


def test_pipeline_nonstreaming_final_applies_single_semif_judgment(monkeypatch) -> None:
    """Non-streaming finals (oneshot / full-audio fallback) route through
    the same bounded SemIf policy: exactly ONE finish() judgment, and the
    accepted text is never labeled user_confirmed."""
    import recordian.streaming_correction as sc

    finished: list[str] = []
    constructed: list[dict[str, object]] = []

    class _SpyCorrector:
        def __init__(self, hotwords, *, endpoint="", timeout_s=0.12, enabled=False, **_):
            constructed.append(
                {"endpoint": endpoint, "timeout_s": timeout_s, "enabled": enabled}
            )

        def finish(self, text):
            finished.append(text)
            return "F:" + text

        def close(self):
            pass

    monkeypatch.setattr(sc, "StreamingHotwordCorrector", _SpyCorrector)
    bus = FakeFcitxBus()
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)

    class _OneshotProvider(_RealtimeProvider):
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):
            self.transcribe_file_calls += 1
            return SimpleNamespace(text="克劳德很强", detected_language="zh")

    lexicon = _RecordingLexicon()
    context = _pipeline_context(
        provider=_OneshotProvider(_ScriptedSession([], finish_text="x")),
        committer=FcitxCommitter(),
        args=_pipeline_args(enable_semif_correction=True, semif_timeout_s=0.2),
        auto_lexicon=lexicon,
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    # Exactly one end-of-sentence judgment on the non-streaming final.
    assert finished == ["克劳德很强"]
    assert constructed and constructed[-1]["enabled"] is True
    # The judged text was committed once through the legacy path...
    texts = [args[0] for method, args in bus.calls if method == "CommitText"]
    assert texts == ["F:克劳德很强"]
    # ...and learned as machine-produced, never user_confirmed. The SemIf
    # judgment changed the text, so the label is "corrected" (still a
    # machine-applied correction, not a user confirmation).
    assert lexicon.calls == [("F:克劳德很强", "corrected")]


# ---------------------------------------------------------------------------
# r5 fixes: empty-stream one-shot recovery on the SAME bound session,
# pre-Begin cancellation gate
# ---------------------------------------------------------------------------

class _EmptyStreamProvider(_RealtimeProvider):
    """File-streaming provider whose stream yields nothing (provider glitch)."""

    def supports_file_streaming(self) -> bool:
        return True

    def transcribe_file_stream(self, audio_path, hotwords):
        return iter(())


def test_pipeline_empty_stream_recovers_oneshot_through_same_session(monkeypatch) -> None:
    """P2-1 fix (positive): empty stream + zero preedit writes must not drop
    the utterance. The one-shot full-audio retry runs and commits through
    the SAME still-bound composition token — never an unbound CommitText."""
    bus = FakeFcitxBus()

    class _OneshotProvider(_EmptyStreamProvider):
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):
            self.transcribe_file_calls += 1
            return SimpleNamespace(text="完整转写结果", detected_language="zh")

    provider = _OneshotProvider(_ScriptedSession([], finish_text="x"))
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    assert provider.transcribe_file_calls == 1
    methods = [method for method, _ in bus.calls]
    # One Begin, zero preedit writes (stream was empty), one same-token commit.
    # The fake bus records the BeginSession token in its session table
    # (the commit keeps the key, only alive flips to False).
    begin_token = next(iter(bus.sessions))
    commits = [tuple(args) for method, args in bus.calls if method == "CommitSession"]
    assert commits == [(begin_token, "完整转写结果")]
    assert "CommitText" not in methods
    assert "CancelSession" not in methods
    result = result_events[0]["result"]
    assert result["text"] == "完整转写结果"
    assert result["commit"]["committed"] is True
    assert result["asr_path"] == "streaming_commit"
    assert any(
        "empty_stream_retry_same_session" in str(e.get("message", "")) for e in state_events
    )


def test_pipeline_empty_stream_retry_focus_lost_refuses_commit(monkeypatch) -> None:
    """P2-1 fix (safety): the user clicks away WHILE the one-shot retry runs.
    The still-bound session refuses the commit (StaleSession) and no
    fallback may write into the new focus — the old unbound-CommitText
    behavior would have landed in the wrong window."""
    bus = FakeFcitxBus()

    class _FocusLostOneshotProvider(_EmptyStreamProvider):
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):
            self.transcribe_file_calls += 1
            # Focus moved during the full-audio retry.
            bus.lose_focus()
            return SimpleNamespace(text="完整转写结果", detected_language="zh")

    provider = _FocusLostOneshotProvider(_ScriptedSession([], finish_text="x"))
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    assert provider.transcribe_file_calls == 1
    methods = [method for method, _ in bus.calls]
    # The commit was attempted once through the ORIGINAL token and refused;
    # nothing was applied anywhere...
    assert methods.count("CommitSession") == 1
    assert bus.applied_commits == []
    # ...no unbound CommitText into the new focus, no second transcription.
    assert "CommitText" not in methods
    assert provider.transcribe_file_calls == 1
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "stale"
    assert result["asr_path"] == "streaming_stale_suppressed"
    # The transcript stays visible for manual copy.
    assert result["text"] == "完整转写结果"


def test_pipeline_empty_stream_retry_failure_is_terminal(monkeypatch) -> None:
    """A FAILED one-shot retry on a bound session is terminal: the session
    is cancelled, the outcome suppresses every further fallback (a retry
    loop could hammer the provider or fall into an unbound commit)."""

    class _FailingOneshotProvider(_EmptyStreamProvider):
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):
            self.transcribe_file_calls += 1
            raise RuntimeError("provider one-shot also failed")

    provider = _FailingOneshotProvider(_ScriptedSession([], finish_text="x"))
    bus = FakeFcitxBus()
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    _patch_pipeline_env(monkeypatch)
    _install_fake_bus(monkeypatch, bus)
    context = _pipeline_context(
        provider=provider,
        committer=FcitxCommitter(),
        args=_pipeline_args(),
        state_events=state_events,
        result_events=result_events,
        error_events=error_events,
    )
    run_postprocess_pipeline(context)

    assert not error_events
    assert provider.transcribe_file_calls == 1  # exactly one retry, no loop
    methods = [method for method, _ in bus.calls]
    assert methods == ["BeginSession", "CancelSession"]
    assert "CommitSession" not in methods and "CommitText" not in methods
    result = result_events[0]["result"]
    assert result["commit"]["committed"] is False
    assert result["commit"]["outcome"] == "cancelled"
    assert result["text"] == ""


def test_worker_cancelled_before_begin_never_binds_session(monkeypatch) -> None:
    """Deadline/cancel arrives while BeginSession is still pending: the
    worker must NOT bind a new session afterwards — nobody could retract
    its preedit writes under the controller's classification. The worker is
    held at the streaming-committer resolve step (before Begin) until the
    cancel is observable, so the ordering is deterministic."""
    bus = FakeFcitxBus()
    events: list[dict[str, object]] = []
    _install_fake_bus(monkeypatch, bus)

    gate = threading.Event()
    monkeypatch.setattr(
        "recordian.realtime_asr.resolve_streaming_committer",
        lambda committer: (gate.wait(timeout=5.0), committer)[1],
    )
    begin_calls: list[object] = []

    def _recording_open(committer):
        begin_calls.append(committer)
        return FcitxCommitter().begin_composition("")

    monkeypatch.setattr("recordian.linux_commit.open_composition_session", _recording_open)

    provider = _RealtimeProvider(_ScriptedSession(["部分"], finish_text="迟到结果"))
    worker = _start_realtime_asr_worker(
        args=_worker_args(),
        provider=provider,
        record_handle=_record_handle(),
        committer=FcitxCommitter(),
        enable_local_commit=True,
        auto_hard_enter=False,
        resolve_hotwords=lambda: [],
        normalize_final_text=lambda text: str(text).strip(),
        on_state=events.append,
    )
    assert worker is not None
    # Cancel strictly before the worker can reach Begin.
    worker.cancel_event.set()
    gate.set()
    worker.thread.join(timeout=2.0)
    assert not worker.thread.is_alive()

    # No session was ever bound, no bus write of any kind happened.
    assert begin_calls == []
    assert bus.calls == []
    assert worker.composition_started is False
    assert worker.composition_active is False
    assert worker.outcome == "cancelled"
    assert worker.commit_info is not None
    assert worker.commit_info["outcome"] == "cancelled"
    assert any(
        "realtime_composition_cancelled_before_begin" in str(e.get("message", ""))
        for e in events
    )
