from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from recordian.eval_capture import (
    DEFAULT_EVAL_CAPTURE_DIR,
    append_eval_sample,
    resolve_eval_capture_dir,
)
from recordian.postprocess_pipeline import _capture_eval_sample


def _read_index(output_dir: Path) -> list[dict]:
    lines = (output_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_append_eval_sample_copies_audio_and_writes_schema(tmp_path: Path) -> None:
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"RIFF-fake-wav-bytes")
    output_dir = tmp_path / "eval-capture"

    payload = append_eval_sample(
        output_dir=output_dir,
        audio_path=audio,
        raw_asr_text="原始识别文本",
        final_text="最终文本",
        record_backend="ffmpeg-pulse",
        source="dictation",
        transcribe_latency_ms=123.4567,
    )

    rows = _read_index(output_dir)
    assert rows == [payload]
    assert payload["raw_asr_text"] == "原始识别文本"
    assert payload["final_text"] == "最终文本"
    assert payload["audio_missing"] is False
    assert payload["gold"] is False
    assert payload["edited_text"] is None
    assert payload["user_edited"] is None
    copied = output_dir / payload["audio_file"]
    assert copied.read_bytes() == b"RIFF-fake-wav-bytes"
    # capture dir is private
    assert (output_dir.stat().st_mode & 0o777) == 0o700


def test_append_eval_sample_missing_audio_still_records_text(tmp_path: Path) -> None:
    output_dir = tmp_path / "eval-capture"
    payload = append_eval_sample(
        output_dir=output_dir,
        audio_path=tmp_path / "gone.wav",
        raw_asr_text="raw",
        final_text="final",
        record_backend="ffmpeg-pulse",
        source="dictation",
        transcribe_latency_ms=1.0,
    )
    assert payload["audio_missing"] is True
    assert payload["audio_file"] is None
    assert _read_index(output_dir) == [payload]


def test_resolve_eval_capture_dir_default() -> None:
    assert resolve_eval_capture_dir("") == Path(DEFAULT_EVAL_CAPTURE_DIR).expanduser()
    assert resolve_eval_capture_dir("  ") == Path(DEFAULT_EVAL_CAPTURE_DIR).expanduser()


def test_pipeline_hook_noop_when_flag_off(tmp_path: Path) -> None:
    args = SimpleNamespace(
        capture_eval_samples=False,
        capture_eval_samples_path=str(tmp_path / "eval-capture"),
        debug_diagnostics=False,
    )
    _capture_eval_sample(
        args=args,
        audio_path=tmp_path / "input.wav",
        record_backend="ffmpeg-pulse",
        raw_text="raw",
        final_text="final",
        transcribe_latency_ms=1.0,
        on_state=lambda event: None,
    )
    assert not (tmp_path / "eval-capture").exists()


def test_pipeline_hook_writes_when_flag_on(tmp_path: Path) -> None:
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"RIFF")
    args = SimpleNamespace(
        capture_eval_samples=True,
        capture_eval_samples_path=str(tmp_path / "eval-capture"),
        debug_diagnostics=False,
    )
    _capture_eval_sample(
        args=args,
        audio_path=audio,
        record_backend="ffmpeg-pulse",
        raw_text="raw",
        final_text="final",
        transcribe_latency_ms=2.0,
        on_state=lambda event: None,
    )
    rows = _read_index(tmp_path / "eval-capture")
    assert len(rows) == 1
    assert rows[0]["final_text"] == "final"
