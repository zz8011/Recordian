import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from recordian.postprocess_pipeline import PostprocessPipelineContext, run_postprocess_pipeline
from recordian.providers import ASRProviderCapabilities


def _base_context(tmp_path: Path) -> tuple[Path, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    audio_path = tmp_path / "input.wav"
    audio_path.write_bytes(b"wav")
    state_events: list[dict[str, object]] = []
    result_events: list[dict[str, object]] = []
    error_events: list[dict[str, object]] = []
    return audio_path, state_events, result_events, error_events


def test_run_postprocess_pipeline_short_circuits_owner_rejection(tmp_path: Path) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _FailProvider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            raise AssertionError("owner gate reject should bypass ASR")

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=321.0,
        owner_filter_enabled=True,
        owner_seen=False,
        owner_last_score=0.42,
        state={},
        provider=_FailProvider(),
        refiner=None,
        committer=SimpleNamespace(backend_name="stdout"),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: text.strip(),
        resolve_hotwords=lambda: ["Recordian"],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert result_events
    payload = result_events[0]["result"]
    assert payload["text"] == ""
    assert payload["commit"]["detail"] == "owner_gate_rejected_no_owner_speech"
    assert "voice_owner_gate_rejected" in str(state_events[0]["message"])


def test_run_postprocess_pipeline_runs_asr_refine_commit_and_lexicon(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        provider_name = "mock-asr"
        capabilities = ASRProviderCapabilities(supports_hotwords=True, supports_context=True)

        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            assert hotwords == ["Recordian", "Docker"]
            return SimpleNamespace(text="DockerDocker", detected_language="en")

    class _Refiner:
        prompt_template = "请整理文本：{text}"

        def refine(self, text: str) -> str:
            assert text == "Docker"
            return "整理后的 Docker"

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    class _AutoLexicon:
        def __init__(self) -> None:
            self.learned: list[str] = []
            self.sources: list[str | None] = []

        def observe_accepted(self, text: str, source: str | None = None) -> list[str]:
            self.learned.append(text)
            self.sources.append(source)
            return ["Docker"]

    auto_lexicon = _AutoLexicon()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.2, -0.2, 0.2, -0.2], dtype=np.float32),
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=True,
            enable_streaming_refine=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=456.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 77},
        provider=_Provider(),
        refiner=_Refiner(),
        committer=_Committer(),
        auto_lexicon=auto_lexicon,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: "Docker" if text == "DockerDocker" else str(text).strip(),
        resolve_hotwords=lambda: ["Recordian", "Docker"],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert result_events
    payload = result_events[0]["result"]
    assert payload["text"] == "整理后的 Docker"
    assert payload["detected_language"] == "en"
    assert payload["asr_provider"] == "mock-asr"
    assert payload["asr_path"] == "oneshot"
    assert payload["asr_capabilities"] == "hotwords,context"
    assert payload["commit"]["committed"] is True
    assert payload["commit"]["detail"] == "committed:整理后的 Docker"
    assert auto_lexicon.learned == ["整理后的 Docker"]
    # Provenance contract: the refiner changed the text, so the observation
    # is labeled "refined" — never a source-less legacy accept.
    assert auto_lexicon.sources == ["refined"]
    assert context.committer.target_window_id == 77
    assert any(event.get("event") == "log" and "ASR 原始输出" in str(event.get("message")) for event in state_events)
    assert any(
        event.get("event") == "log"
        and "diag finalize" in str(event.get("message"))
        and "asr_provider=mock-asr" in str(event.get("message"))
        and "detected_language=en" in str(event.get("message"))
        for event in state_events
    )


def test_run_postprocess_pipeline_captures_refine_samples_jsonl(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)
    capture_path = tmp_path / "refine-samples.jsonl"

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="原始 识别 文本")

    class _Refiner:
        prompt_template = "请整理文本：{text}"
        model_name = "mock-refiner"

        def refine(self, text: str) -> str:
            assert text == "原始 识别 文本"
            return "整理后的文本"

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.2], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=True,
            enable_streaming_refine=False,
            capture_refine_samples=True,
            capture_refine_samples_path=str(capture_path),
            refine_preset="default",
            refine_provider="local",
            enable_streaming_commit=False,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=222.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={},
        provider=_Provider(),
        refiner=_Refiner(),
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert capture_path.exists()
    lines = capture_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["raw_asr_text"] == "原始 识别 文本"
    assert payload["final_text"] == "整理后的文本"
    assert payload["refine_applied"] is True
    assert payload["refine_changed"] is True
    assert payload["refine_model"] == "mock-refiner"
    assert payload["refine_enabled"] is False
    assert payload["refiner_ready"] is True
    assert any("diag refine_sample_captured=" in str(event.get("message", "")) for event in state_events)


def test_run_postprocess_pipeline_logs_when_refine_enabled_without_refiner(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)
    capture_path = tmp_path / "refine-samples.jsonl"

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="嗯 测试 测试")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.2], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_text_refine=True,
            enable_streaming_refine=False,
            capture_refine_samples=True,
            capture_refine_samples_path=str(capture_path),
            refine_preset="intent",
            refine_provider="cloud",
            enable_streaming_commit=False,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=222.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert any("text_refine_enabled_but_unavailable" in str(event.get("message", "")) for event in state_events)
    payload = json.loads(capture_path.read_text(encoding="utf-8").strip())
    assert payload["refine_applied"] is False
    assert payload["refine_enabled"] is True
    assert payload["refiner_ready"] is False


def test_run_postprocess_pipeline_corrects_prefetched_text_before_oneshot_commit(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            raise AssertionError("prefetched ASR text should skip re-transcription")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    committer = _Committer()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.2, -0.2, 0.2, -0.2], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=False,
            enable_remote_paste=False,
            enable_hotword_correction=True,
            asr_context="CodeX -> Codex",
            hotword=["Codex"],
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=456.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 77},
        provider=_Provider(),
        refiner=None,
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: ["Codex"],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
        prefetched_asr_text="回去开 CodeX",
        prefetched_detected_language="zh",
        prefetched_transcribe_latency_ms=123.0,
        prefetched_commit_info=None,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert committer.calls == ["回去开 Codex"]
    payload = result_events[0]["result"]
    assert payload["text"] == "回去开 Codex"
    assert payload["asr_path"] == "prefetched"
    assert any("hotword_corrected" in str(event.get("message", "")) for event in state_events)


def test_run_postprocess_pipeline_reuses_prefetched_asr_text_and_commit(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            raise AssertionError("prefetched ASR text should skip re-transcription")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            raise AssertionError("prefetched realtime commit should skip local recommit")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.2, -0.2, 0.2, -0.2], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=456.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 77},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
        prefetched_asr_text="实时识别成功",
        prefetched_detected_language="zh",
        prefetched_transcribe_latency_ms=123.0,
        prefetched_commit_info={"backend": "xdotool", "committed": True, "detail": "realtime_chunks:3"},
    )

    run_postprocess_pipeline(context)

    assert not error_events
    payload = result_events[0]["result"]
    assert payload["text"] == "实时识别成功"
    assert payload["detected_language"] == "zh"
    assert payload["asr_path"] == "prefetched"
    assert payload["transcribe_latency_ms"] == 123.0
    assert payload["commit"]["detail"] == "realtime_chunks:3"


def test_run_postprocess_pipeline_skips_local_refine_recommit_when_realtime_commit_exists(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            raise AssertionError("prefetched ASR text should skip re-transcription")

    class _Refiner:
        prompt_template = "请整理文本：{text}"

        def refine_stream(self, text: str):
            raise AssertionError("prefetched realtime commit should suppress refine local recommit")

        def refine(self, text: str) -> str:
            return "整理后的文本"

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            raise AssertionError("local recommit should be skipped")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.2, -0.2, 0.2, -0.2], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=456.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 77},
        provider=_Provider(),
        refiner=_Refiner(),
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
        prefetched_asr_text="实时预稿",
        prefetched_transcribe_latency_ms=123.0,
        prefetched_commit_info={"backend": "xdotool", "committed": True, "detail": "realtime_chunks:4"},
    )

    run_postprocess_pipeline(context)

    assert not error_events
    payload = result_events[0]["result"]
    assert payload["text"] == "整理后的文本"
    assert payload["commit"]["detail"] == "realtime_chunks:4"


def test_run_postprocess_pipeline_waits_before_hard_enter_for_clipboard_paste(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="测试文本")

    class _Committer:
        backend_name = "xdotool-clipboard"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="xdotool-clipboard", committed=True, detail="paste:ctrl+v")

    delays: list[float] = []

    class _EnterResult:
        committed = True
        detail = "hard_enter_sent"

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr("recordian.postprocess_pipeline.time.sleep", lambda value: delays.append(value))
    monkeypatch.setattr("recordian.postprocess_pipeline.send_hard_enter", lambda committer: _EnterResult())

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=True,
            debug_diagnostics=False,
            enable_streaming_refine=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert delays
    assert delays[0] > 0.0


def test_run_postprocess_pipeline_streams_asr_commit_when_enabled(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file_stream(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            yield "你"
            yield "好"

    class _Committer:
        backend_name = "xdotool"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="xdotool", committed=True, detail=f"typed:{text}")

    committer = _Committer()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    # New safe contract: legacy (non-composition) backends get preview-only
    # streaming and exactly ONE final commit — no per-keystroke typing.
    assert committer.calls == ["你好"]
    assert result_events[0]["result"]["text"] == "你好"
    assert any(event.get("event") == "stream_partial" for event in state_events)


def test_empty_file_stream_falls_back_to_oneshot_commit(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file_stream(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            if False:
                yield ""
            return
            yield ""

        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="整句还在")

    class _Committer:
        backend_name = "fcitx"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="fcitx", committed=True, detail="committed")

    committer = _Committer()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )
    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
            enable_hotword_correction=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
        prefetched_commit_info={"backend": "fcitx", "committed": False, "detail": "realtime_complete"},
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert committer.calls == ["整句还在"]
    assert result_events[0]["result"]["asr_path"] == "oneshot"
    assert result_events[0]["result"]["commit"]["committed"] is True


def test_run_postprocess_pipeline_streams_asr_commit_with_normalized_growth(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file_stream(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            yield "Docker"
            yield "DockerDocker"

    class _Committer:
        backend_name = "xdotool"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="xdotool", committed=True, detail=f"typed:{text}")

    committer = _Committer()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: "Docker" if text == "DockerDocker" else str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert committer.calls == ["Docker"]
    assert result_events[0]["result"]["text"] == "Docker"


def test_run_postprocess_pipeline_streams_asr_commit_uses_streaming_committer_override(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file_stream(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            yield "你"
            yield "好"

    class _OriginalCommitter:
        backend_name = "xdotool-clipboard"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            raise AssertionError("original clipboard committer should not handle streaming chunks")

    class _FastCommitter:
        backend_name = "xdotool"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="xdotool", committed=True, detail=f"typed:{text}")

    original_committer = _OriginalCommitter()
    fast_committer = _FastCommitter()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.resolve_streaming_committer",
        lambda committer: fast_committer,
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=original_committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert original_committer.calls == []
    # Override committer is used, but the legacy key-stream is gone: a
    # single final commit instead of per-chunk synthetic typing.
    assert original_committer.calls == []
    assert fast_committer.calls == ["你好"]
    assert result_events[0]["result"]["text"] == "你好"


def test_run_postprocess_pipeline_streams_refine_commit_when_enabled(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="原始文本")

    class _Refiner:
        prompt_template = "请整理文本：{text}"

        def refine_stream(self, text: str):
            yield "整理"
            yield "完成"

    class _Committer:
        backend_name = "xdotool"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="xdotool", committed=True, detail=f"typed:{text}")

    committer = _Committer()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=_Refiner(),
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    # Refine streaming previews via state events; the commit happens
    # exactly once with the full refined text.
    assert committer.calls == ["整理完成"]
    assert result_events[0]["result"]["text"] == "整理完成"
    assert any(event.get("event") == "refine_stream_chunk" for event in state_events)


def test_run_postprocess_pipeline_streams_refine_commit_applies_postprocess_rule(tmp_path: Path, monkeypatch) -> None:
    """P2-2 fix: streaming refine finalizes through the preset's deterministic
    @postprocess cleanup (zh-stutter-lite), not just the non-streaming path."""
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="原始文本")

    class _Refiner:
        prompt_template = "请整理文本：{text}"
        last_refine_skipped = True  # stale flag from a previous utterance

        def refine_stream(self, text: str):
            yield "我"
            yield "我"
            yield "想"

    class _Committer:
        backend_name = "xdotool"
        target_window_id = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit(self, text: str) -> SimpleNamespace:
            self.calls.append(text)
            return SimpleNamespace(backend="xdotool", committed=True, detail=f"typed:{text}")

    committer = _Committer()
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=True,
            enable_remote_paste=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=_Refiner(),
        committer=committer,
        auto_lexicon=None,
        refine_postprocess_rule="zh-stutter-lite",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    # The refine stream stuttered ("我我" → "我"); the @postprocess rule now
    # applies at streaming finalization too, and the commit sees the
    # cleaned text exactly once.
    assert committer.calls == ["我想"]
    assert result_events[0]["result"]["text"] == "我想"
    assert any(event.get("event") == "refine_stream_chunk" for event in state_events)
    # The diagnostic marker is set consistently for the streaming path too.
    assert context.refiner.last_refine_skipped is False


def test_run_postprocess_pipeline_records_remote_paste_result(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="跨电脑文本")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {
            "enabled": True,
            "attempted": True,
            "sent": True,
            "host": "192.168.5.111",
            "detail": "ok",
        },
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_remote_paste=True,
            remote_paste_host="192.168.5.111",
            remote_paste_port=24872,
            remote_paste_timeout_s=3.0,
            deskflow_log_path="",
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    payload = result_events[0]["result"]
    assert payload["commit"]["committed"] is True
    assert payload["commit"]["remote_paste"]["sent"] is True


def test_run_postprocess_pipeline_routes_to_remote_only_when_deskflow_screen_matches(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)
    state_path = tmp_path / "active_screen.json"
    state_path.write_text(
        json.dumps({"screen": "remote-screen", "server_name": "server-screen", "updated_at": "2026-03-15T00:00:00Z"}),
        encoding="utf-8",
    )

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="远端专用文本")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            raise AssertionError("remote-only route should skip local commit")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.1], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {
            "enabled": True,
            "attempted": True,
            "sent": True,
            "host": "192.168.5.111",
            "detail": "ok",
            "routing_mode": "remote-only",
        },
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_remote_paste=True,
            remote_paste_host="192.168.5.111",
            remote_paste_port=24872,
            remote_paste_timeout_s=3.0,
            remote_paste_follow_deskflow_active_screen=True,
            deskflow_active_screen_path=str(state_path),
            deskflow_log_path="",
            remote_paste_screen_name="remote-screen",
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=111.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={"target_window_id": 88},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    payload = result_events[0]["result"]
    assert payload["commit"]["backend"] == "remote-paste"
    assert payload["commit"]["committed"] is True
    assert payload["commit"]["detail"] == "ok"
    assert payload["commit"]["remote_paste"]["routing_mode"] == "remote-only"


def test_run_postprocess_pipeline_applies_hotword_correction_before_commit(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        provider_name = "mock-asr"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="把 Githup 的 CodeX 配置看一下", detected_language="zh")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.2, -0.2, 0.2, -0.2], dtype=np.float32),
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_hotword_correction=True,
            hotword_correction_edits=1,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=100.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: ["github", "Codex"],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    payload = result_events[0]["result"]
    assert payload["text"] == "把 github 的 Codex 配置看一下"
    assert any(
        event.get("event") == "log" and "hotword_corrected" in str(event.get("message"))
        for event in state_events
    )


def test_run_postprocess_pipeline_hotword_correction_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        provider_name = "mock-asr"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return SimpleNamespace(text="把 Githup 的配置看一下", detected_language="zh")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.2, -0.2, 0.2, -0.2], dtype=np.float32),
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_hotword_correction=False,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=100.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={},
        provider=_Provider(),
        refiner=None,
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="none",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: ["github"],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    payload = result_events[0]["result"]
    assert payload["text"] == "把 Githup 的配置看一下"


def test_run_refinement_skips_llm_for_long_text() -> None:
    from recordian.postprocess_pipeline import _run_refinement

    refine_calls: list[str] = []

    class _Refiner:
        prompt_template = None

        def refine(self, text: str) -> str:  # noqa: ANN001
            refine_calls.append(str(text))
            raise AssertionError("long text should never reach the LLM refiner")

    state_events: list[dict[str, object]] = []
    args = argparse.Namespace(
        refine_max_len_llm=50,
        enable_streaming_refine=False,
        debug_diagnostics=False,
        config_path="",
    )
    long_text = "要要要把这个功能做出来并且测试通过，然后我们就发布上线。然后然后我们再复查一遍。" * 5
    assert len(long_text) > 50

    refiner = _Refiner()
    text, latency_ms = _run_refinement(
        args=args,
        refiner=refiner,
        text=long_text,
        effective_hotwords=[],
        refine_postprocess_rule="zh-stutter-lite",
        on_state=state_events.append,
    )

    assert refine_calls == []
    assert latency_ms == 0.0
    assert refiner.last_refine_skipped is True
    assert "要把这个功能做出来" in text
    assert len(text) < len(long_text)
    assert any("refine_skip_long_text" in str(event.get("message", "")) for event in state_events)


def test_run_refinement_still_refines_short_text_below_threshold() -> None:
    from recordian.postprocess_pipeline import _run_refinement

    refine_calls: list[str] = []

    class _Refiner:
        prompt_template = None

        def refine(self, text: str) -> str:  # noqa: ANN001
            refine_calls.append(str(text))
            return "整理后的短文"

    state_events: list[dict[str, object]] = []
    args = argparse.Namespace(
        refine_max_len_llm=800,
        enable_streaming_refine=False,
        debug_diagnostics=False,
        config_path="",
    )
    short_text = "这个这个方案要测试通过"
    assert len(short_text) <= 800

    refiner = _Refiner()
    text, latency_ms = _run_refinement(
        args=args,
        refiner=refiner,
        text=short_text,
        effective_hotwords=[],
        refine_postprocess_rule="none",
        on_state=state_events.append,
    )

    assert refine_calls == [short_text]
    assert refiner.last_refine_skipped is False
    assert text == "整理后的短文"
    assert latency_ms >= 0.0


def test_should_skip_llm_refine_respects_threshold_and_off_value() -> None:
    from recordian.postprocess_pipeline import _should_skip_llm_refine

    args = argparse.Namespace(refine_max_len_llm=800)
    assert _should_skip_llm_refine(args, "短") is False
    assert _should_skip_llm_refine(args, "长" * 800) is False
    assert _should_skip_llm_refine(args, "长" * 801) is True

    off_args = argparse.Namespace(refine_max_len_llm=0)
    assert _should_skip_llm_refine(off_args, "长" * 5000) is False


def test_run_postprocess_pipeline_skips_llm_and_commits_cleaned_long_text(tmp_path: Path, monkeypatch) -> None:
    audio_path, state_events, result_events, error_events = _base_context(tmp_path)

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            raw = "这个这个方案没问题，然后然后我们按这个执行，确保确保流程顺畅。" * 8
            return SimpleNamespace(text=raw, detected_language="zh")

    refine_calls: list[str] = []

    class _Refiner:
        prompt_template = None

        def refine(self, text: str) -> str:  # noqa: ANN001
            refine_calls.append(str(text))
            raise AssertionError("long text should skip LLM refine in the full pipeline")

    class _Committer:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail=f"committed:{text}")

    monkeypatch.setattr(
        "recordian.postprocess_pipeline.read_wav_mono_f32",
        lambda path: np.array([0.3, -0.2, 0.2], dtype=np.float32),
    )
    monkeypatch.setattr(
        "recordian.postprocess_pipeline.send_remote_paste_from_args",
        lambda args, text, *, log=None: {"enabled": False},
    )

    context = PostprocessPipelineContext(
        args=argparse.Namespace(
            config_path="",
            auto_hard_enter=False,
            debug_diagnostics=False,
            enable_streaming_refine=False,
            enable_streaming_commit=False,
            enable_remote_paste=False,
            refine_max_len_llm=50,
        ),
        audio_path=audio_path,
        record_backend="ffmpeg-pulse",
        record_latency_ms=100.0,
        owner_filter_enabled=False,
        owner_seen=False,
        owner_last_score=-1.0,
        state={},
        provider=_Provider(),
        refiner=_Refiner(),
        committer=_Committer(),
        auto_lexicon=None,
        refine_postprocess_rule="zh-stutter-lite",
        normalize_final_text=lambda text: str(text).strip(),
        resolve_hotwords=lambda: [],
        on_state=state_events.append,
        on_result=result_events.append,
        on_error=error_events.append,
    )

    run_postprocess_pipeline(context)

    assert not error_events
    assert refine_calls == []
    payload = result_events[0]["result"]
    assert payload["text"]
    assert payload["refine_latency_ms"] == 0.0
    assert payload["commit"]["committed"] is True
    assert "确保流程顺畅" in payload["text"]
    assert "按这个执行" in payload["text"]
    assert len(payload["text"]) < 8 * len("这个这个方案没问题，然后然后我们按这个执行，确保确保流程顺畅。")
    assert any("refine_skip_long_text" in str(event.get("message", "")) for event in state_events)
