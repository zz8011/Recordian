import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from recordian.postprocess_pipeline import PostprocessPipelineContext, run_postprocess_pipeline


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
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            assert hotwords == ["Recordian", "Docker"]
            return SimpleNamespace(text="DockerDocker")

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

        def observe_accepted(self, text: str) -> list[str]:
            self.learned.append(text)
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
    assert payload["commit"]["committed"] is True
    assert payload["commit"]["detail"] == "committed:整理后的 Docker"
    assert auto_lexicon.learned == ["整理后的 Docker"]
    assert context.committer.target_window_id == 77
    assert any(event.get("event") == "log" and "ASR 原始输出" in str(event.get("message")) for event in state_events)


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
