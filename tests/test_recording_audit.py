"""Recording audit regressions: synthetic audio, no desktop or live services."""
from __future__ import annotations

import json
import threading
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from test_continuous_dictation import _FakeCompositionSession, _FakeProvider, _Harness, _Reader
from test_duration_guard import _HttpCloudProvider, _make_ptt
from test_streaming_preedit import (
    FakeFcitxBus,
    _install_fake_bus,
    _pipeline_args,
    _pipeline_context,
    _RealtimeProvider,
    _record_handle,
    _ScriptedSession,
    _worker_args,
)

from recordian import audio, linux_commit, recording_controller, wayland_desktop
from recordian import postprocess_pipeline as pipeline
from recordian.realtime_asr import _start_realtime_asr_worker


@pytest.fixture(autouse=True)
def _isolate_external_io(monkeypatch):
    monkeypatch.setattr(recording_controller, "begin_output_mute", lambda: None)
    monkeypatch.setattr(wayland_desktop, "select_desktop_committer", lambda committer: committer)
    monkeypatch.setattr(
        pipeline, "send_remote_paste_from_args", lambda *args, **kwargs: {"enabled": False},
    )


def test_cancel_during_final_preedit_never_commits(monkeypatch):
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    entered, resume = threading.Event(), threading.Event()
    original_call = bus.call

    def blocked_call(method, signature, args):
        if method == "UpdatePreedit" and args[1] == "最终文本":
            entered.set()
            assert resume.wait(3)
        return original_call(method, signature, args)

    monkeypatch.setattr(linux_commit, "_fcitx_busctl_call", blocked_call)
    asr = _ScriptedSession([""], finish_text="最终文本")
    worker = _start_realtime_asr_worker(
        args=_worker_args(), provider=_RealtimeProvider(asr), record_handle=_record_handle(1),
        committer=linux_commit.FcitxCommitter(), enable_local_commit=True,
        auto_hard_enter=False, resolve_hotwords=lambda: [],
        normalize_final_text=lambda text: text, on_state=lambda event: None,
    )
    assert worker is not None
    try:
        assert entered.wait(3)
        worker.cancel_event.set()
        worker.cancel_session()
    finally:
        resume.set()
        worker.thread.join(3)
    assert not worker.thread.is_alive()
    assert bus.applied_commits == []
    assert worker.outcome == "cancelled"
    assert all(not entry["alive"] for entry in bus.sessions.values())


@pytest.mark.parametrize("during_processing", [False, True])
def test_rejected_start_preserves_capture_target(monkeypatch, during_processing):
    harness = _make_ptt(monkeypatch, _HttpCloudProvider())
    focus = [101]
    monkeypatch.setattr(recording_controller, "get_focused_window_id", lambda: focus[0])
    entered, resume = threading.Event(), threading.Event()
    contexts = []

    def blocked_pipeline(context):
        contexts.append(context)
        entered.set()
        assert resume.wait(3)

    monkeypatch.setattr(recording_controller, "run_postprocess_pipeline", blocked_pipeline)
    try:
        assert harness.start()
        if during_processing:
            assert harness.stop()
            assert entered.wait(3)
        focus[0] = 202
        assert not harness.start()
        if not during_processing:
            assert harness.stop()
            assert entered.wait(3)
        assert contexts[0].state["target_window_id"] == 101
        assert contexts[0].committer.target_window_id == 101
    finally:
        resume.set()
        harness.exit_daemon()


@pytest.mark.parametrize("carried_session", [False, True])
def test_interrupted_refinement_preserves_entire_asr(monkeypatch, carried_session):
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    committer = linux_commit.FcitxCommitter()
    session = committer.begin_composition("") if carried_session else None

    class InterruptedRefiner:
        prompt_template = None

        def refine_stream(self, text):
            yield "第一项。"
            raise TimeoutError("synthetic stream interruption")

    provider = _RealtimeProvider(_ScriptedSession([]))
    results, errors = [], []
    context = _pipeline_context(
        provider=provider, committer=committer,
        args=_pipeline_args(enable_streaming_commit=False, enable_streaming_refine=True),
        prefetched_asr_text="第一项。第二项。第三项。",
        prefetched_commit_info={"committed": False, "outcome": "released_for_refine"} if carried_session else None,
        prefetched_outcome="released_for_refine" if carried_session else "no_composition",
        composition_session=session, refiner=InterruptedRefiner(),
        result_events=results, error_events=errors,
    )
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    assert results[0]["result"]["text"] == "第一项。第二项。第三项。"
    method = "CommitSession" if carried_session else "CommitText"
    writes = [args[-1] for name, args in bus.calls if name == method]
    assert writes == ["第一项。第二项。第三项。"]
    assert provider.transcribe_file_calls == 0


def test_failed_bound_refine_stream_keeps_original_without_fallback(monkeypatch):
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)

    class InterruptedRefiner:
        prompt_template = None

        def refine_stream(self, text):
            yield "第一项。"
            raise TimeoutError("synthetic stream interruption")

    results, errors = [], []
    context = _pipeline_context(
        provider=_RealtimeProvider(_ScriptedSession([])), committer=linux_commit.FcitxCommitter(),
        args=_pipeline_args(), prefetched_asr_text="第一项。第二项。第三项。",
        refiner=InterruptedRefiner(), result_events=results, error_events=errors,
    )
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    assert results[0]["result"]["text"] == "第一项。第二项。第三项。"
    assert not results[0]["result"]["commit"]["committed"]
    assert bus.applied_commits == []
    assert not any(method == "CommitText" for method, args in bus.calls)
    assert all(not entry["alive"] for entry in bus.sessions.values())


def test_buffered_continuous_text_survives_trailing_silence(tmp_path):
    samples = np.concatenate((np.full(16000, 0.01, dtype=np.float32), np.zeros(24 * 16000, dtype=np.float32)))
    session = _FakeCompositionSession()
    session.supports_segments = False
    harness = _Harness(
        reader=_Reader(samples.tobytes()), session=session, refine_enabled=True,
        provider=_FakeProvider(finals=["完整原文。", ""], partials=["完整原文。", ""]),
    )
    worker = harness.run()
    assert worker.outcome == "released_for_refine"
    assert worker.final_text == "完整原文。"
    wav_path = tmp_path / "quiet-speech.wav"
    audio.write_wav_mono_f32(wav_path, samples)
    results, errors = [], []
    context = _pipeline_context(
        provider=harness.provider, committer=SimpleNamespace(backend_name="fcitx"),
        args=_pipeline_args(), prefetched_asr_text=worker.final_text,
        prefetched_commit_info=worker.commit_info, prefetched_outcome=worker.outcome,
        composition_session=session, prefetched_semif_applied=True,
        result_events=results, error_events=errors,
    )
    context.audio_path = wav_path
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    assert session.commits == ["完整原文。"]
    assert results[0]["result"]["text"] == "完整原文。"


@pytest.mark.parametrize("outcome", [
    "committed", "released_for_refine", "no_composition", "stale", "uncertain",
    "cancelled", "suppressed", "uncertain_prefix",
])
def test_prefetched_turn_never_rescans_or_replays_audio(monkeypatch, outcome):
    opened, writes = [], []

    def unexpected_open(*args, **kwargs):
        opened.append(args)
        raise AssertionError("prefetched turn must not read WAV")

    monkeypatch.setattr(audio.wave, "open", unexpected_open)
    session = _FakeCompositionSession()
    status = "uncertain" if outcome == "uncertain_prefix" else outcome

    def commit(text):
        writes.append(text)
        return linux_commit.CommitResult("fake", True, "committed")

    provider = _RealtimeProvider(_ScriptedSession([]))
    results, errors = [], []
    context = _pipeline_context(
        provider=provider, committer=SimpleNamespace(backend_name="fake", commit=commit),
        args=_pipeline_args(enable_streaming_commit=False), prefetched_asr_text="保留原文。",
        prefetched_commit_info={
            "committed": outcome in {"committed", "uncertain_prefix"}, "outcome": status,
            "segments_committed": 1 if outcome == "uncertain_prefix" else 0,
        },
        prefetched_outcome=status, prefetched_composition_started=status != "no_composition",
        composition_session=session if status == "released_for_refine" else None,
        result_events=results, error_events=errors,
    )
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    assert opened == []
    assert provider.transcribe_file_calls == 0
    assert results[0]["result"]["text"] == "保留原文。"
    assert writes == (["保留原文。"] if status == "no_composition" else [])
    assert session.commits == (["保留原文。"] if status == "released_for_refine" else [])


@pytest.mark.parametrize("silence", [False, True])
def test_fallback_silence_scan_reads_bounded_blocks(monkeypatch, silence):
    requested = []

    class VirtualWave:
        remaining = 120 * 16000

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getnchannels(self):
            return 1

        def getsampwidth(self):
            return 2

        def getframerate(self):
            return 16000

        def getnframes(self):
            return 120 * 16000

        def readframes(self, count):
            requested.append(count)
            size = min(count, self.remaining)
            self.remaining -= size
            # Only the last second has speech; the gate must scan all blocks.
            voiced = min(size, 16000) if not silence and self.remaining == 0 else 0
            return b"\x00\x00" * (size - voiced) + b"\x00\x40" * voiced

    monkeypatch.setattr(audio.wave, "open", lambda *args, **kwargs: VirtualWave())
    results, errors = [], []
    provider = _RealtimeProvider(_ScriptedSession([]))
    context = _pipeline_context(
        provider=provider, committer=SimpleNamespace(
            backend_name="fake", commit=lambda text: linux_commit.CommitResult("fake", True),
        ), args=_pipeline_args(enable_streaming_commit=False),
        result_events=results, error_events=errors,
    )
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    assert requested and max(requested) <= 16000
    assert provider.transcribe_file_calls == (0 if silence else 1)
    assert results[0]["result"]["text"] == ("" if silence else "全文兜底")


@pytest.mark.parametrize("frame, expected", [
    ([16384], 0.5), ([16384, 0], 0.25), ([16384, -16384], 0.0),
])
def test_chunked_rms_preserves_pcm16_mono_downmix(tmp_path, frame, expected):
    path = tmp_path / "rms.wav"
    samples = np.tile(np.array(frame, dtype="<i2"), (32001, 1))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(len(frame))
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    assert audio.wav_mono_rms(path) == pytest.approx(expected)


def test_empty_wav_rms_retains_inconclusive_result(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"")
    assert np.isnan(audio.wav_mono_rms(path))


@pytest.mark.parametrize("route", ["oneshot_refine", "carried_stream_refine", "file_stream_refine"])
@pytest.mark.parametrize("capture_samples", [False, True])
def test_refinement_logs_counts_while_ui_and_opt_in_capture_keep_text(
    monkeypatch, tmp_path, route, capture_samples,
):
    original = "合成审计原文 synthetic_private_ASR_7f4e。"
    refined = "合成审计精炼 synthetic_private_refined_4a9b。"
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    monkeypatch.setattr(pipeline, "wav_mono_rms", lambda path: 0.2)
    capture_path = tmp_path / "explicit-refine-capture.jsonl"

    class Provider:
        def transcribe_file(self, audio_path, hotwords):
            return SimpleNamespace(text=original, detected_language="zh")

    class Refiner:
        prompt_template = None

        def refine(self, text):
            assert text == original
            return refined

        def refine_stream(self, text):
            assert text == original
            yield refined[:8]
            yield refined[8:]

    committer = linux_commit.FcitxCommitter()
    carried = route == "carried_stream_refine"
    session = committer.begin_composition("") if carried else None
    events, results, errors = [], [], []
    context = _pipeline_context(
        provider=Provider(), committer=committer, refiner=Refiner(),
        args=_pipeline_args(
            enable_streaming_commit=route != "oneshot_refine", enable_streaming_refine=carried,
            capture_refine_samples=capture_samples, capture_refine_samples_path=str(capture_path),
        ),
        prefetched_asr_text=original if carried else "",
        prefetched_commit_info={"committed": False, "outcome": "released_for_refine"} if carried else None,
        prefetched_outcome="released_for_refine" if carried else "",
        composition_session=session, state_events=events, result_events=results, error_events=errors,
    )
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    logs = [str(event.get("message", "")) for event in events if event.get("event") == "log"]
    assert all(original not in message and refined not in message for message in logs)
    assert f"ASR 原始输出: text_chars={len(original)}" in logs
    assert f"精炼后输出: text_chars={len(refined)}" in logs
    assert results[0]["result"]["text"] == refined
    if route != "oneshot_refine":
        chunks = [event for event in events if event.get("event") == "refine_stream_chunk"]
        assert "".join(event["chunk"] for event in chunks) == refined
        assert chunks[-1]["accumulated"] == refined
    if capture_samples:
        saved = [json.loads(line) for line in capture_path.read_text().splitlines()]
        assert len(saved) == 1
        assert saved[0]["raw_asr_text"] == original
        assert saved[0]["final_text"] == refined
    else:
        assert not capture_path.exists()


def test_file_asr_ui_partial_keeps_text_without_full_text_logs(monkeypatch):
    text = "合成流式正文 synthetic_private_partial_628f。"
    bus = FakeFcitxBus()
    _install_fake_bus(monkeypatch, bus)
    monkeypatch.setattr(pipeline, "wav_mono_rms", lambda path: 0.2)

    class Provider:
        def transcribe_file_stream(self, audio_path, hotwords):
            yield text

    events, results, errors = [], [], []
    context = _pipeline_context(
        provider=Provider(), committer=linux_commit.FcitxCommitter(), args=_pipeline_args(),
        state_events=events, result_events=results, error_events=errors,
    )
    pipeline.run_postprocess_pipeline(context)
    assert not errors
    assert results[0]["result"]["text"] == text
    assert any(event.get("event") == "stream_partial" and event.get("text") == text for event in events)
    assert all(text not in str(event.get("message", "")) for event in events if event.get("event") == "log")
