"""Virtual-audio tests for the continuous Alt dictation loop.

No GPU, no microphone, no DBus, no real sockets: the capture reader, the
ASR sessions and the composition session are scripted fakes. The formatter
is the REAL ``streaming_correction.corrector_from_args`` and the display /
commit normaliser is the REAL ``text_cleanup._normalize_final_text``, so the
raw number/URL seam is tested against the production formatting chain, not
against fake rules. Timing constants come from duration_guard and are
monkeypatched down where a short segment helps.
"""
from __future__ import annotations

import argparse
import array
import io
import threading
import time
from types import SimpleNamespace

import pytest

from recordian import duration_guard as guard
from recordian.continuous_dictation import join_raw_tail, run_continuous_dictation
from recordian.linux_dictate import MonitorOverflowError
from recordian.realtime_asr import _RealtimeASRWorkerHandle, _split_held_tail
from recordian.streaming_correction import corrector_from_args
from recordian.text_cleanup import _normalize_final_text

RATE = 16000
BPS = 4  # f32 mono
CHUNK_SAMPLES = 1600  # 0.1 s
CHUNK_BYTES = CHUNK_SAMPLES * BPS

_CORRECTOR_ARGS = argparse.Namespace(
    semif_endpoint="",
    semif_timeout_s=0.12,
    enable_semif_correction=False,
    contextual_aliases=[],
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _pcm(seconds: float, amp: float) -> bytes:
    n = int(round(seconds * RATE))
    return (array.array("f", [amp]) * n).tobytes()


def _speech(seconds: float) -> bytes:
    return _pcm(seconds, 0.5)


def _silence(seconds: float) -> bytes:
    return _pcm(seconds, 0.0)


class _Reader:
    """One scripted capture stream. read() counts every byte it hands out."""

    def __init__(self, payload: bytes) -> None:
        self._buf = io.BytesIO(payload)
        self.bytes_read = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        out = self._buf.read(size)
        self.bytes_read += len(out)
        return out

    def close(self) -> None:
        self.closed = True


class _OverflowReader:
    def __init__(self, payload: bytes, fail_after: int) -> None:
        self._buf = io.BytesIO(payload)
        self.bytes_read = 0
        self.fail_after = fail_after

    def read(self, size: int = -1) -> bytes:
        if self.bytes_read >= self.fail_after:
            raise MonitorOverflowError("monitor reader backlog exceeded (test)")
        out = self._buf.read(size)
        self.bytes_read += len(out)
        return out

    def close(self) -> None:
        pass


class _FakeASRSession:
    def __init__(self, final_text: str, partial: str = "") -> None:
        self.final_text = final_text
        self.partial = partial
        self.samples = 0
        self.heard_speech = False
        self.finished = 0
        self.cancelled = 0
        self.elapsed_ms = 1.0

    def push_audio(self, raw: bytes, *, block: bool = False, timeout_s: float | None = None) -> dict[str, str]:
        self.samples += len(raw) // BPS
        if raw.strip(b"\x00"):
            self.heard_speech = True
        return {"text": self.partial}

    def finish(self) -> SimpleNamespace:
        self.finished += 1
        return SimpleNamespace(text=self.final_text, detected_language="zh", elapsed_ms=1.0)

    def cancel(self) -> None:
        self.cancelled += 1


class _FakeProvider:
    provider_name = "confucius-asr"

    def __init__(
        self,
        finals: list[str] | None = None,
        partials: list[str] | None = None,
        *,
        speech_final: str = "",
    ) -> None:
        self.finals = list(finals or [])
        self.partials = list(partials or [])
        # Final text for sessions beyond the scripted list that heard speech
        # (long-silence tests rotate many empty sockets before the tail).
        self.speech_final = speech_final
        self.sessions: list[_FakeASRSession] = []
        self.open_calls: list[dict[str, object]] = []
        self.fail_on_open: dict[int, Exception] = {}

    def start_realtime_session(
        self,
        *,
        hotwords: list[str],
        prefix_context: str = "",
        cancel_event: threading.Event | None = None,
    ) -> _FakeASRSession:
        index = len(self.open_calls)
        self.open_calls.append(
            {"hotwords": hotwords, "prefix_context": prefix_context, "cancel_event": cancel_event}
        )
        if index in self.fail_on_open:
            raise self.fail_on_open[index]
        final = self.finals[index] if index < len(self.finals) else None
        partial = self.partials[index] if index < len(self.partials) else ""
        session = _LateBindingASRSession(final, partial, self.speech_final)
        self.sessions.append(session)
        return session


class _LateBindingASRSession(_FakeASRSession):
    """Final text may depend on whether the session heard any speech."""

    def __init__(self, final_text: str | None, partial: str, speech_final: str) -> None:
        super().__init__(final_text or "", partial)
        self._fixed_final = final_text
        self._speech_final = speech_final

    def finish(self) -> SimpleNamespace:
        if self._fixed_final is None:
            self.final_text = self._speech_final if self.heard_speech else ""
        return super().finish()


class _Result:
    def __init__(self, committed: bool, outcome: str = "", detail: str = "") -> None:
        self.committed = committed
        self.outcome = outcome or ("committed" if committed else "uncertain")
        self.detail = detail


class _FakeCompositionSession:
    supports_segments = True
    active = True

    def __init__(self) -> None:
        self.preedits: list[str] = []
        self.segments: list[str] = []
        self.commits: list[str] = []
        self.cancelled = 0
        self.preedit_result: _Result | None = None
        self.segment_result: _Result | None = None
        self.commit_result: _Result | None = None
        self.fail_segment_at: int | None = None
        self.fail_segment_outcome = "uncertain"

    def update_preedit(self, text: str) -> _Result:
        self.preedits.append(text)
        if self.preedit_result is not None:
            return self.preedit_result
        return _Result(True)

    def commit_segment(self, text: str) -> _Result:
        self.segments.append(text)
        if self.fail_segment_at is not None and len(self.segments) == self.fail_segment_at:
            return _Result(False, outcome=self.fail_segment_outcome, detail="test_segment_failure")
        if self.segment_result is not None:
            return self.segment_result
        return _Result(True, detail=f"segment {len(self.segments)} ")

    def commit(self, text: str) -> _Result:
        self.commits.append(text)
        if self.commit_result is not None:
            return self.commit_result
        return _Result(True)

    def cancel(self) -> None:
        self.cancelled += 1


class _Harness:
    def __init__(
        self,
        *,
        reader: object,
        provider: _FakeProvider,
        session: _FakeCompositionSession | None = None,
        refine_enabled: bool = False,
        hotwords: list[str] | None = None,
        build_corrector: object = None,
    ) -> None:
        self.reader = reader
        self.provider = provider
        self.session = session or _FakeCompositionSession()
        self.events: list[dict[str, object]] = []
        self.fatals: list[str] = []
        self.corrector_contexts: list[str] = []
        self.refine_enabled = refine_enabled
        self.hotwords = list(hotwords or [])
        self.cancel_event = threading.Event()
        self.worker = _RealtimeASRWorkerHandle(thread=threading.Thread(target=lambda: None))
        self._build_corrector_override = build_corrector

    def build_corrector(self, context: str = "") -> object:
        self.corrector_contexts.append(context)
        if self._build_corrector_override is not None:
            return self._build_corrector_override(context)
        # REAL factory (SemIf disabled): deterministic hotword correction.
        return corrector_from_args(_CORRECTOR_ARGS, self.hotwords, context=context)

    def run(self) -> _RealtimeASRWorkerHandle:
        run_continuous_dictation(
            worker=self.worker,
            session=self.session,
            provider=self.provider,
            reader=self.reader,
            cancel_event=self.cancel_event,
            args=_CORRECTOR_ARGS,
            chunk_bytes=CHUNK_BYTES,
            sample_rate=RATE,
            channels=1,
            build_corrector=self.build_corrector,
            resolve_hotwords=lambda: self.hotwords,
            normalize_final_text=_normalize_final_text,  # REAL normaliser
            on_state=self.events.append,
            on_capture_fatal=self.fatals.append,
            refine_enabled=self.refine_enabled,
            auto_hard_enter=False,
            streaming_committer=SimpleNamespace(backend_name="fcitx"),
        )
        return self.worker

    def segment_events(self) -> list[dict[str, object]]:
        return [e for e in self.events if e.get("event") == "segment_end"]


# ---------------------------------------------------------------------------
# Held-tail splitting / joining (unit)
# ---------------------------------------------------------------------------

def test_split_held_tail_holds_raw_number_url_and_word() -> None:
    assert _split_held_tail("价格是一百二") == ("价格是", "一百二")
    assert _split_held_tail("参见www点exa") == ("参见", "www点exa")
    assert _split_held_tail("版本3.14") == ("版本", "3.14")
    assert _split_held_tail("say hel") == ("say ", "hel")
    assert _split_held_tail("今天天气不错。") == ("今天天气不错。", "")


def test_split_held_tail_caps_at_limit() -> None:
    prefix, held = _split_held_tail("x" + "7" * 100, limit=64)
    assert len(held) == 64
    assert prefix == "x" + "7" * 36


def test_split_held_tail_keeps_yao_digit() -> None:
    # 幺 is the spoken "1" of ports / IDs: it must survive the cut as a digit
    # instead of being committed alone.
    assert _split_held_tail("前文幺一") == ("前文", "幺一")
    assert _split_held_tail("端口幺") == ("", "端口幺")


def test_split_held_tail_keeps_percent_and_number_markers_whole() -> None:
    # A marker that only means something with the digits after it is never
    # committed on its own: the next segment completes it before formatting
    # ("百分之三" + "十五" -> "百分之三十五" -> 35%).
    assert _split_held_tail("前文百分之三") == ("前文", "百分之三")
    assert _split_held_tail("比例百分之") == ("比例", "百分之")
    # ASCII digits after the marker belong to the same held span: the next
    # segment's "5" must join "百分之3" before the formatter runs.
    assert _split_held_tail("比例百分之3") == ("比例", "百分之3")
    # 号码 must stay glued to its digits: a bare "三四" would be an
    # approximation and stay unconverted.
    assert _split_held_tail("前文号码三") == ("前文", "号码三")
    assert _split_held_tail("前文编号幺二") == ("前文", "编号幺二")
    # A connector (是/为) and an optional space stay with the marker span:
    # committing "号码是" alone loses the ID context across the cut.
    assert _split_held_tail("号码是三") == ("", "号码是三")
    assert _split_held_tail("前文号码 三") == ("前文", "号码 三")
    # A bare marker with no digits yet is an ordinary word, not a tail.
    assert _split_held_tail("这是号码") == ("这是号码", "")


def test_join_raw_tail_keeps_yao_percent_and_marker_raw() -> None:
    # join_raw_tail concatenates RAW text: no ASCII/percent conversion and no
    # marker rewriting may happen before the next segment is joined.
    assert join_raw_tail("幺", "二三四") == "幺二三四"
    assert join_raw_tail("百分之三", "十五") == "百分之三十五"
    assert join_raw_tail("号码三", "四") == "号码三四"
    assert join_raw_tail("端口幺", "二三四") == "端口幺二三四"


def test_join_raw_tail_number_and_url_concatenate() -> None:
    assert join_raw_tail("一百二", "十五") == "一百二十五"
    assert join_raw_tail("www点exa", "mple点com") == "www点example点com"
    assert join_raw_tail("3.1", "4159") == "3.14159"


def test_join_raw_tail_english_word_gets_one_space() -> None:
    assert join_raw_tail("hel", "world") == "hel world"
    assert join_raw_tail("hello", " world") == "hello world"
    assert join_raw_tail("", "text") == "text"
    assert join_raw_tail("tail", "") == "tail"


# ---------------------------------------------------------------------------
# Full rotation: 90 s virtual audio, exact samples, 4 sessions
# ---------------------------------------------------------------------------

def test_ninety_seconds_rotate_exact_samples_and_single_capture() -> None:
    payload = (
        _speech(20.0) + _silence(0.4)   # soft cut at 20.4 s
        + _speech(24.0)                 # hard cut at 24 s
        + _speech(23.6) + _silence(0.4)  # cut at 24 s
        + _speech(21.6)                 # EOF tail
    )
    assert len(payload) == 90 * RATE * BPS
    provider = _FakeProvider(["第一段。", "第二段。", "第三段。", "最后一段。"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # One capture stream, every byte consumed exactly once.
    assert h.reader.bytes_read == len(payload)
    # Four ASR sockets, none beyond the 24 s hard cap, every sample assigned.
    assert len(provider.sessions) == 4
    samples = [s.samples for s in provider.sessions]
    assert samples == [int(20.4 * RATE), 24 * RATE, 24 * RATE, int(21.6 * RATE)]
    assert sum(samples) == 90 * RATE
    assert all(s <= int(guard.CONTINUOUS_SEGMENT_MAX_S * RATE) for s in samples)
    # Three segment commits on the SAME token, then exactly one final commit.
    assert h.session.segments == ["第一段。", "第二段。", "第三段。"]
    assert h.session.commits == ["最后一段。"]
    assert h.session.cancelled == 0
    # Continuous partials streamed into the preedit, each within the bound.
    assert len(h.session.preedits) >= 4
    assert all(len(p) <= guard.CONTINUOUS_PREEDIT_CHARS for p in h.session.preedits)
    # Later sockets carry the bounded committed prefix as context.
    assert provider.open_calls[1].get("prefix_context") == "第一段。"
    assert provider.open_calls[2].get("prefix_context") == "第一段。第二段。"
    # segment_end events carry sample counts, no transcript.
    seg_events = h.segment_events()
    assert len(seg_events) == 4
    assert sum(int(e["audio_samples"]) for e in seg_events) == 90 * RATE
    assert all("text" not in e for e in seg_events)
    assert worker.outcome == "committed"
    assert worker.segments_committed == 3
    assert worker.commit_info["committed"] is True
    assert worker.final_text == "第一段。第二段。第三段。最后一段。"
    assert h.fatals == []


# ---------------------------------------------------------------------------
# Raw held tail across a boundary, through the REAL formatting chain
# ---------------------------------------------------------------------------

def _short_segment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "CONTINUOUS_SEGMENT_MIN_S", 1.0)
    monkeypatch.setattr(guard, "CONTINUOUS_SEGMENT_MAX_S", 3.0)
    monkeypatch.setattr(guard, "CONTINUOUS_SILENCE_S", 0.3)


def test_raw_chinese_numeral_tail_formats_as_one_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["价格是一百二", "十五元"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # The raw tail "一百二" was NOT committed at the boundary; the combined
    # raw snapshot "一百二十五" formatted once via the REAL normaliser.
    assert h.session.segments == ["价格是"]
    assert h.session.commits == ["125元"]
    assert worker.final_text == "价格是125元"
    assert h.fatals == []


def test_raw_url_tail_joins_without_space(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["参见www点exa", "mple点com。"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    assert h.session.segments == ["参见"]
    assert h.session.commits == ["www.example.com。"]
    assert worker.final_text == "参见www.example.com。"


def test_real_corrector_hotword_applies_at_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["他叫露西。", "明天见。"])
    h = _Harness(reader=_Reader(payload), provider=provider, hotwords=["露西→Lucy"])
    worker = h.run()

    # The REAL corrector_from_args factory applied the replacement pair.
    assert h.session.segments == ["他叫Lucy。"]
    assert h.session.commits == ["明天见。"]
    assert worker.outcome == "committed"


def test_display_normalisation_does_not_rewrite_held_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partials are normalised for display only; the held tail must stay raw
    for the next segment's combined snapshot."""
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["一共一百二", "十五"], partials=["一共一百二", "一百二十五"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # Boundary committed only the safe prefix; the raw tail joined the next
    # segment and formatted once into "125".
    assert h.session.segments == ["一共"]
    assert h.session.commits == ["125"]
    assert worker.final_text == "一共125"


# ---------------------------------------------------------------------------
# Raw held tail across a boundary: 幺 / 百分数 / explicit number markers
# ---------------------------------------------------------------------------

def test_boundary_next_yi_stays_chinese_and_port_tail_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The e2e counterexample shape: at the segment cut after
    「下一个是端口幺」, the prefix used to commit 「下1个是」 and the held
    port digits were at risk."""
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["下一个是端口幺", "二完毕"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # The 定位 prefix stayed Chinese and the held port digits still formed
    # one number when the next segment arrived.
    assert h.session.segments == ["下一个是"]
    assert h.session.commits == ["端口12完毕"]
    assert worker.final_text == "下一个是端口12完毕"
    assert "下1个" not in worker.final_text
    assert h.fatals == []


def test_boundary_keeps_yao_digit_for_next_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["端口幺", "二三四"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # Nothing was formatted at the cut: the raw tail reached the formatter
    # once as the joined snapshot "端口幺二三四".
    assert h.session.segments == []
    assert h.session.commits == ["端口1234"]
    assert worker.final_text == "端口1234"
    assert h.fatals == []


def test_boundary_keeps_percent_marker_for_next_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["比例百分之三", "十五"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    expected_tail = _normalize_final_text("百分之三十五")
    assert expected_tail.endswith(("%", "％")), expected_tail
    # Only the safe prefix "比例" crossed the cut; "百分之" was never
    # committed, so the next segment's "十五" completed the percentage in one
    # formatter pass over the raw snapshot "百分之三十五".
    assert h.session.segments == ["比例"]
    assert h.session.commits == [expected_tail]
    assert worker.final_text == "比例" + expected_tail
    assert h.fatals == []


def test_boundary_keeps_number_marker_for_next_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["号码三", "四"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # Had "号码" been committed alone, the tail would have been a bare "三四"
    # approximation and stayed unconverted.
    assert h.session.segments == []
    assert h.session.commits == ["号码34"]
    assert worker.final_text == "号码34"
    assert h.fatals == []


def test_boundary_keeps_ascii_percent_for_next_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["比例百分之3", "5"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # "百分之3" was held raw; the next segment's "5" joined it and the pair
    # formatted once as a single percentage.
    assert h.session.segments == ["比例"]
    assert h.session.commits == ["35%"]
    assert worker.final_text == "比例35%"
    assert h.fatals == []


def test_boundary_keeps_marker_connector_and_digits_for_next_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["号码是三", "四"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    # The whole "号码是三" span stayed raw, so the joined snapshot still had
    # the ID marker and read "号码是34" instead of a bare "三四" approximation.
    assert h.session.segments == []
    assert h.session.commits == ["号码是34"]
    assert worker.final_text == "号码是34"
    assert h.fatals == []


# ---------------------------------------------------------------------------
# Busy model at handoff: fail closed, keep the committed prefix
# ---------------------------------------------------------------------------

def test_busy_rejection_at_rotation_keeps_prefix_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    from recordian.providers.confucius_asr import ConfuciusBusyRejected

    payload = _speech(1.5) + _silence(0.4) + _speech(1.0)
    provider = _FakeProvider(["第一段。"])
    provider.fail_on_open[1] = ConfuciusBusyRejected("busy")
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    assert h.session.segments == ["第一段。"]
    assert h.session.commits == [], "no final commit after a rejected handoff"
    assert len(provider.open_calls) == 2, "no extra sockets beyond the rejected one"
    assert worker.segments_committed == 1
    assert worker.commit_info["committed"] is True
    assert "continuous_prefix_kept" in str(worker.commit_info["detail"])
    assert worker.outcome == "uncertain"
    assert h.fatals, "fatal callback must stop the microphone"
    assert h.session.cancelled >= 1


# ---------------------------------------------------------------------------
# EOF during handoff: one tail flush, no empty new socket
# ---------------------------------------------------------------------------

def test_eof_right_after_boundary_opens_no_empty_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    # Ends exactly at a soft cut (silence run == the 0.3 s setting, and the
    # stream is exhausted on the same chunk): nothing remains for a tail.
    payload = _speech(1.5) + _silence(0.3)
    provider = _FakeProvider(["第一段。"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    assert len(provider.open_calls) == 1, "an empty tail must not open a new socket"
    assert h.session.segments == ["第一段。"]
    assert h.session.commits == [], "no empty final commit"
    assert h.session.cancelled == 1
    assert worker.outcome == "committed"
    assert worker.commit_info["detail"] == "continuous_tail_empty"


def test_eof_mid_segment_flushes_tail_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.8)
    provider = _FakeProvider(["第一段。", "尾巴。"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    assert len(provider.sessions) == 2
    assert provider.sessions[1].finished == 1, "tail flush happens exactly once"
    assert h.session.commits == ["尾巴。"]
    assert worker.outcome == "committed"


# ---------------------------------------------------------------------------
# Cancel paths: zero preedit/segment/final writes after the cancel lands
# ---------------------------------------------------------------------------

def test_cancel_mid_stream_commits_nothing_and_retracts(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(10.0)
    provider = _FakeProvider(["永远不会提交"])
    h = _Harness(reader=_Reader(payload), provider=provider)

    original_push = _FakeASRSession.push_audio

    def _push_then_cancel(self: _FakeASRSession, raw: bytes, **kwargs: object) -> dict[str, str]:
        if self.samples >= 3 * CHUNK_SAMPLES:
            h.cancel_event.set()
        return original_push(self, raw, **kwargs)

    monkeypatch.setattr(_FakeASRSession, "push_audio", _push_then_cancel)
    worker = h.run()

    assert worker.outcome == "cancelled"
    assert worker.commit_info["committed"] is False
    assert worker.commit_info["outcome"] == "cancelled"
    assert h.session.commits == []
    assert h.session.segments == []
    assert h.session.cancelled >= 1
    assert provider.sessions[0].cancelled >= 1
    assert h.fatals == [], "a user/controller cancel is not a fatal capture error"


def test_cancel_during_formatter_finish_makes_zero_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cancel lands while the boundary corrector finish() is blocking:
    no new preedit, no commit_segment afterwards."""
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(5.0)
    provider = _FakeProvider(["第一段。"], speech_final="尾巴。")

    class _CancellingCorrector:
        def __init__(self, event: threading.Event) -> None:
            self._event = event

        def submit(self, text: str) -> str:
            return text

        def poll(self, text: str) -> str:
            return text

        def finish(self, text: str) -> str:
            self._event.set()
            return text

        def cancel(self) -> None:
            pass

        def close(self) -> None:
            pass

    h = _Harness(
        reader=_Reader(payload),
        provider=provider,
        build_corrector=lambda context="": _CancellingCorrector(h.cancel_event),
    )
    worker = h.run()

    assert worker.outcome == "cancelled"
    assert h.session.segments == []
    assert h.session.commits == []
    # The only preedit writes happened before the cancel landed (partial
    # display); the canonical segment text was never shown or committed.
    assert "第一段。" not in h.session.preedits
    assert "尾巴。" not in h.session.preedits
    assert h.session.cancelled >= 1


def test_cancel_after_segment_keeps_prefix_reports_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(10.0)
    provider = _FakeProvider(["第一段。", "第二段"])
    h = _Harness(reader=_Reader(payload), provider=provider)

    original_push = _FakeASRSession.push_audio

    def _push_then_cancel(self: _FakeASRSession, raw: bytes, **kwargs: object) -> dict[str, str]:
        if len(h.provider.sessions) > 1 and self is h.provider.sessions[1] and self.samples >= 2 * CHUNK_SAMPLES:
            h.cancel_event.set()
        return original_push(self, raw, **kwargs)

    monkeypatch.setattr(_FakeASRSession, "push_audio", _push_then_cancel)
    worker = h.run()

    assert h.session.segments == ["第一段。"]
    assert h.session.commits == []
    assert worker.outcome == "cancelled"
    assert worker.commit_info["committed"] is True
    assert worker.commit_info["segments_committed"] == 1
    assert h.session.cancelled >= 1


# ---------------------------------------------------------------------------
# Bounded fanout overflow
# ---------------------------------------------------------------------------

def test_monitor_overflow_is_explicit_and_fatal() -> None:
    payload = _speech(30.0)
    provider = _FakeProvider(["不会到达"])
    reader = _OverflowReader(payload, fail_after=3 * CHUNK_BYTES)
    h = _Harness(reader=reader, provider=provider)
    worker = h.run()

    assert "monitor_backlog_overflow" in worker.error
    assert worker.outcome == "uncertain"
    assert h.session.commits == []
    assert h.session.segments == []
    assert h.fatals == ["monitor_backlog_overflow"]


# ---------------------------------------------------------------------------
# Stale / uncertain IME outcomes: no duplicates, no retry, no fallback
# ---------------------------------------------------------------------------

def test_stale_preedit_stops_without_any_commit() -> None:
    payload = _speech(1.0)
    session = _FakeCompositionSession()
    session.preedit_result = _Result(False, outcome="stale", detail="preedit_stale")
    provider = _FakeProvider(["文本"])
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)
    worker = h.run()

    assert worker.session_stale is True
    assert worker.outcome == "stale"
    assert session.segments == []
    assert session.commits == []
    assert h.fatals == ["ime_stale"]


def test_uncertain_segment_reply_is_terminal_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(1.0)
    session = _FakeCompositionSession()
    session.fail_segment_at = 1
    session.fail_segment_outcome = "uncertain"
    provider = _FakeProvider(["第一段。", "第二段。"])
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)
    worker = h.run()

    assert len(session.segments) == 1, "an unknown segment reply is never retried"
    assert session.commits == []
    assert worker.outcome == "uncertain"
    assert worker.commit_info["committed"] is False
    assert h.fatals
    # The failure is terminal: the second socket is never opened.
    assert len(provider.open_calls) == 1


def test_unknown_final_commit_reply_never_retried() -> None:
    payload = _speech(0.5)
    session = _FakeCompositionSession()
    session.commit_result = _Result(False, outcome="uncertain", detail="reply_lost")
    provider = _FakeProvider(["尾巴"])
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)
    worker = h.run()

    assert session.commits == ["尾巴"], "exactly one final commit attempt"
    assert worker.outcome == "uncertain"
    assert worker.commit_info["committed"] is False
    assert h.fatals


def test_stale_segment_with_prefix_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(1.5) + _silence(0.4) + _speech(0.5)
    session = _FakeCompositionSession()
    session.fail_segment_at = 2
    session.fail_segment_outcome = "stale"
    provider = _FakeProvider(["第一段。", "第二段。", "第三段。"])
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)
    worker = h.run()

    # Segment 1 committed; segment 2 went stale: prefix preserved, terminal.
    assert session.segments == ["第一段。", "第二段。"]
    assert session.commits == []
    assert worker.session_stale is True
    assert worker.segments_committed == 1
    assert worker.commit_info["committed"] is True
    assert worker.commit_info["segments_committed"] == 1
    assert "continuous_prefix_kept" in str(worker.commit_info["detail"])
    assert h.fatals
    # Nothing beyond the stale segment is attempted.
    assert len(provider.open_calls) == 2


def test_preedit_never_exceeds_bound() -> None:
    payload = _speech(1.0)
    long_partial = "字" * 2000
    provider = _FakeProvider(["终稿"], partials=[long_partial])
    h = _Harness(reader=_Reader(payload), provider=provider)
    h.run()

    assert h.session.preedits, "partial preedit expected"
    assert max(len(p) for p in h.session.preedits) <= guard.CONTINUOUS_PREEDIT_CHARS


def test_corrector_context_is_bounded_committed_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_segment_settings(monkeypatch)
    long_first = "文" * 300 + "。"
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider([long_first, "尾。"])
    h = _Harness(reader=_Reader(payload), provider=provider)
    worker = h.run()

    assert worker.outcome == "committed"
    # The corrector for the final tail judged with a bounded context only.
    assert h.corrector_contexts, "corrector was built at least once"
    assert all(len(c) <= guard.CONTINUOUS_CONTEXT_CHARS for c in h.corrector_contexts)
    # Provider prefix_context is the same bounded committed tail.
    assert provider.open_calls[1].get("prefix_context") == long_first[-guard.CONTINUOUS_CONTEXT_CHARS:]


def test_refine_released_only_when_no_segment_committed() -> None:
    payload = _speech(0.5)
    provider = _FakeProvider(["一次说完。"])
    h = _Harness(reader=_Reader(payload), provider=provider, refine_enabled=True)
    worker = h.run()

    assert worker.outcome == "released_for_refine"
    assert worker.composition_session is h.session
    assert h.session.commits == [], "the pipeline owns the single refined commit"
    assert h.session.cancelled == 0


# ---------------------------------------------------------------------------
# IME token TTL keepalive during long silence (>120 s native inactivity TTL)
# ---------------------------------------------------------------------------

def test_long_silence_refreshes_same_token_then_commits() -> None:
    # Production 30 s audio-counted refresh; well under the 120 s token TTL.
    payload = _silence(130.0) + _speech(0.5)
    provider = _FakeProvider(speech_final="句号。")
    session = _FakeCompositionSession()
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)
    worker = h.run()

    refreshes = [p for p in session.preedits if p == ""]
    assert len(refreshes) >= 4, (
        f"130 s of silence must re-touch the same token ~every "
        f"{guard.CONTINUOUS_IME_REFRESH_S:.0f}s, got {len(refreshes)}"
    )
    # The post-silence speech still lands on the SAME session object (no new
    # Begin) and commits exactly once.
    assert session.commits == ["句号。"]
    assert worker.outcome == "committed"
    assert h.fatals == []


def test_refresh_failure_during_silence_fails_closed() -> None:
    payload = _silence(35.0)
    session = _FakeCompositionSession()
    provider = _FakeProvider()
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)

    calls = {"n": 0}
    original_update = session.update_preedit

    def _flaky_update(text: str) -> _Result:
        calls["n"] += 1
        # First refresh (empty preedit at ~30 s of silence) goes stale.
        if text == "":
            return _Result(False, outcome="stale", detail="preedit_stale")
        return original_update(text)

    session.update_preedit = _flaky_update  # type: ignore[method-assign]
    worker = h.run()

    assert worker.outcome == "stale"
    assert worker.session_stale is True
    assert h.fatals == ["ime_stale"]
    assert session.commits == []


# ---------------------------------------------------------------------------
# Same-snapshot partial poll: a ready SemIf judgment reaches the preedit
# before the segment boundary / finish
# ---------------------------------------------------------------------------

_SEMIF_ARGS = argparse.Namespace(
    semif_endpoint="http://127.0.0.1:9/v1/systemone",
    semif_timeout_s=0.5,
    enable_semif_correction=True,
    contextual_aliases=[],
)


class _PeakedResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _BlockingJudgeSession:
    """One controllable SemIf request: post() blocks until released, then
    answers with a peaked choice for the first candidate."""

    def __init__(self, release: threading.Event, entered: threading.Event) -> None:
        self._release = release
        self._entered = entered
        self.calls = 0
        self.closed = False

    def post(
        self, url: str, json: dict | None = None, timeout: float | None = None
    ) -> _PeakedResponse:
        self.calls += 1
        self._entered.set()
        assert self._release.wait(timeout=5.0), "SemIf request was never released"
        criteria = json["questions"]["pick"]["criteria"]  # type: ignore[index]
        probabilities = dict.fromkeys(criteria, 0.0)
        probabilities["c0"] = 1.0
        return _PeakedResponse(
            {"answers": {"pick": {"type": "choice", "choice": "c0", "probabilities": probabilities}}}
        )

    def close(self) -> None:
        self.closed = True


def test_same_snapshot_partial_poll_shows_ready_judgment_before_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delayed SemIf result must land in the preedit BEFORE finish():
    repeated partials poll the original snapshot instead of only re-reading
    the deterministic submit() text."""
    pytest.importorskip("pypinyin")
    from recordian import semif_judge

    _short_segment_settings(monkeypatch)
    release = threading.Event()
    entered = threading.Event()
    judge = _BlockingJudgeSession(release, entered)
    # The REAL corrector/session wiring, with only the HTTP session faked.
    monkeypatch.setattr(semif_judge, "require_requests", lambda: SimpleNamespace(Session=lambda: judge))

    text = "桌上有石器。"
    provider = _FakeProvider([text], partials=[text])
    built: list[object] = []

    def _build(context: str = "") -> object:
        corrector = corrector_from_args(_SEMIF_ARGS, ["时期"], context=context)
        built.append(corrector)
        return corrector

    session = _FakeCompositionSession()
    h = _Harness(reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)), provider=provider,
                 session=session, build_corrector=_build)

    # Each preedit is stamped with how many ASR finish() calls had happened:
    # the ready correction must be displayed before the first finish.
    stamped: list[tuple[str, int]] = []
    original_update = session.update_preedit

    def _update(value: str) -> _Result:
        stamped.append((value, sum(s.finished for s in provider.sessions)))
        return original_update(value)

    session.update_preedit = _update  # type: ignore[method-assign]

    pushes = {"n": 0}
    original_push = _FakeASRSession.push_audio

    def _push_then_release(self: _FakeASRSession, raw: bytes, **kwargs: object) -> dict[str, str]:
        pushes["n"] += 1
        if pushes["n"] == 3:
            assert entered.wait(timeout=5.0), "SemIf request never started"
            release.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if built and built[-1].poll(text) == "桌上有时期。":  # type: ignore[attr-defined]
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("ready SemIf correction never became pollable")
        return original_push(self, raw, **kwargs)

    monkeypatch.setattr(_FakeASRSession, "push_audio", _push_then_release)
    worker = h.run()

    # One request for the one snapshot: the repeated partial only polls it,
    # and the boundary finish() reuses the same judged result.
    assert judge.calls == 1
    assert ("桌上有时期。", 0) in stamped, "correction was not shown before finish()"
    assert session.segments == ["桌上有时期。"]
    assert session.commits == []
    assert worker.outcome == "committed"
    assert worker.final_text == "桌上有时期。"
    assert h.fatals == []


_ALIAS_SEMIF_ARGS = argparse.Namespace(
    semif_endpoint="http://127.0.0.1:9/v1/systemone",
    semif_timeout_s=0.35,
    enable_semif_correction=True,
    contextual_aliases=[{"heard": "jeff", "word": "jev", "meaning": "软件工具"}],
)


class _RoleJudgeSession:
    """Role judge for the REAL corrector: every eligible clause is "tool"."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.closed = False

    def post(
        self, url: str, json: dict | None = None, timeout: float | None = None
    ) -> _PeakedResponse:
        self.payloads.append(json)  # type: ignore[arg-type]
        questions = json["questions"]  # type: ignore[index]
        assert set(questions) == {"role"}, questions
        return _PeakedResponse(
            {
                "answers": {
                    "role": {
                        "type": "choice",
                        "choice": "tool",
                        "probabilities": {"tool": 1.0, "person": 0.0, "unclear": 0.0},
                    }
                }
            }
        )

    def close(self) -> None:
        self.closed = True


def test_segment_with_twin_software_clauses_commits_jev_at_the_real_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recordian-7bv at the real commit boundary: the 31 s fixture shape has
    two identical software clauses in one segment. The old global uniqueness
    filter sent zero requests and committed raw "jeff" twice; one
    clause-scoped verdict must now cover both spans before commit."""
    pytest.importorskip("pypinyin")
    from recordian import semif_judge

    _short_segment_settings(monkeypatch)
    judge = _RoleJudgeSession()
    monkeypatch.setattr(
        semif_judge, "require_requests", lambda: SimpleNamespace(Session=lambda: judge)
    )

    segment = "服务器上的jeff模型怎么样，端口1234，服务器上的jeff模型怎么样"
    provider = _FakeProvider([segment, "服务器上的jeff模型怎么样，比例35%。"])
    h = _Harness(
        reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)),
        provider=provider,
        build_corrector=lambda context="": corrector_from_args(
            _ALIAS_SEMIF_ARGS, [], context=context
        ),
    )
    worker = h.run()

    fixed = "服务器上的jev模型怎么样，端口1234，服务器上的jev模型怎么样"
    assert h.session.segments == [fixed]
    assert h.session.commits == ["服务器上的jev模型怎么样，比例35%。"]
    assert worker.final_text == fixed + "服务器上的jev模型怎么样，比例35%。"
    assert worker.outcome == "committed"
    # One verdict per segment: the twin clauses share the identical clause text.
    assert len(judge.payloads) == 2, judge.payloads
    assert h.fatals == []


def test_segment_never_rewrites_a_bare_helper_person_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live SemIf failure at the real commit boundary: the model answered
    "tool" for both clauses (0.97 / 0.74) and the person Jeff was committed
    as jev. The explicitly modified "jeff工具" clause is still corrected; the
    bare helper alias is gated before any request."""
    pytest.importorskip("pypinyin")
    from recordian import semif_judge

    _short_segment_settings(monkeypatch)
    judge = _RoleJudgeSession()
    monkeypatch.setattr(
        semif_judge, "require_requests", lambda: SimpleNamespace(Session=lambda: judge)
    )

    segment = "打开jeff工具检查这个项目，Jeff帮我调试脚本"
    provider = _FakeProvider([segment, "记录完成。"])
    h = _Harness(
        reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)),
        provider=provider,
        build_corrector=lambda context="": corrector_from_args(
            _ALIAS_SEMIF_ARGS, [], context=context
        ),
    )
    worker = h.run()

    fixed = "打开jev工具检查这个项目，Jeff帮我调试脚本"
    assert h.session.segments == [fixed]
    assert h.session.commits == ["记录完成。"]
    assert worker.final_text == fixed + "记录完成。"
    assert worker.outcome == "committed"
    # One judgeable clause; the bare helper alias is never sent.
    assert len(judge.payloads) == 1, judge.payloads
    assert h.fatals == []


class _RecordingCorrector:
    def __init__(self) -> None:
        self.submits: list[str] = []
        self.polls: list[str] = []
        self.closed = False

    def submit(self, text: str) -> str:
        self.submits.append(text)
        return text

    def poll(self, text: str) -> str:
        self.polls.append(text)
        return text

    def finish(self, text: str) -> str:
        return text

    def close(self) -> None:
        self.closed = True


def test_repeated_text_in_a_new_segment_is_submitted_not_polled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot marker is per corrector: after a segment commit the
    corrector is rebuilt, so a repeated phrase must start a fresh judgment
    instead of being mistaken for a still-pending same-snapshot poll."""
    _short_segment_settings(monkeypatch)
    payload = _speech(1.5) + _silence(0.4) + _speech(0.5)
    provider = _FakeProvider(["重复。", "重复。"], partials=["重复。"] * 40)
    built: list[_RecordingCorrector] = []

    def _build(context: str = "") -> object:
        corrector = _RecordingCorrector()
        built.append(corrector)
        return corrector

    h = _Harness(reader=_Reader(payload), provider=provider, build_corrector=_build)
    worker = h.run()

    assert len(built) >= 2, "a new corrector is built after the segment commit"
    assert built[0].closed is True
    assert built[1].submits == ["重复。"], (
        "the first partial of the new segment must submit, not poll a stale snapshot"
    )
    assert h.session.segments == ["重复。"]
    assert worker.outcome == "committed"


# ---------------------------------------------------------------------------
# Idle keepalive must never replay an already committed prefix
# ---------------------------------------------------------------------------

def test_idle_keepalive_after_segment_commit_never_replays_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_segment_settings(monkeypatch)
    monkeypatch.setattr(guard, "CONTINUOUS_IME_REFRESH_S", 0.5)
    payload = _speech(1.5) + _silence(2.5) + _speech(0.5)
    provider = _FakeProvider(["第一段。"], speech_final="尾巴。")
    session = _FakeCompositionSession()
    h = _Harness(reader=_Reader(payload), provider=provider, session=session)

    committed_at: list[int] = []
    original_segment = session.commit_segment

    def _segment(value: str) -> _Result:
        result = original_segment(value)
        if result.committed:
            committed_at.append(len(session.preedits))
        return result

    session.commit_segment = _segment  # type: ignore[method-assign]
    worker = h.run()

    assert session.segments == ["第一段。"]
    assert committed_at, "the first segment must commit"
    after = session.preedits[committed_at[0]:]
    assert any(p == "" for p in after), "the idle keepalive must re-touch the same token"
    assert all("第一段。" not in p for p in after), (
        "a committed prefix must never be pushed back into the preedit"
    )
    assert session.cancelled == 0, "the keepalive stays on the same composition token"
    assert worker.segments_committed == 1
    assert worker.final_text == "第一段。尾巴。"
    assert h.fatals == []


# ---------------------------------------------------------------------------
# Sub-frame reads: bounded alignment, no spin, no duplicated samples
# ---------------------------------------------------------------------------

class _ShortReadReader:
    """Delivers scripted short reads before falling back to full chunks."""

    def __init__(self, payload: bytes, cuts: list[int]) -> None:
        self._buf = io.BytesIO(payload)
        self._cuts = list(cuts)
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        take = self._cuts.pop(0) if self._cuts else size
        out = self._buf.read(take)
        self.bytes_read += len(out)
        return out

    def close(self) -> None:
        pass


def _run_with_deadline(h: _Harness, timeout: float = 5.0) -> None:
    done = threading.Event()

    def _run() -> None:
        h.run()
        done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert done.wait(timeout), "the continuous loop hung on a short read"
    thread.join(timeout=1.0)


def test_short_read_partial_frame_realigns_without_duplicating_samples() -> None:
    payload = _speech(0.5)
    provider = _FakeProvider(["短读。"])
    reader = _ShortReadReader(payload, cuts=[2, 1])
    h = _Harness(reader=reader, provider=provider)
    _run_with_deadline(h)

    # Every byte was read exactly once and every full sample pushed exactly
    # once: the 3-byte residue was carried, never replayed.
    assert reader.bytes_read == len(payload)
    assert sum(s.samples for s in provider.sessions) == len(payload) // BPS
    assert h.session.commits == ["短读。"]
    assert h.worker.outcome == "committed"
    assert h.fatals == []


def test_short_read_trailing_partial_frame_is_dropped_not_padded() -> None:
    # Three bytes: less than one f32 frame, then EOF. No complete sample
    # exists, so none may be padded out of the residue.
    provider = _FakeProvider(speech_final="不会到达")
    h = _Harness(reader=_ShortReadReader(b"\x01\x02\x03", cuts=[3]), provider=provider)
    _run_with_deadline(h)

    assert all(s.samples == 0 for s in provider.sessions)
    assert all(not s.heard_speech for s in provider.sessions)
    assert h.session.segments == []
    assert h.session.commits == []
    assert h.worker.outcome == "cancelled"
    assert h.fatals == []
