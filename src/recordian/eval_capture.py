from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_EVAL_CAPTURE_DIR = "~/.local/share/recordian/eval-capture"


def resolve_eval_capture_dir(value: object) -> Path:
    raw = str(value).strip() or DEFAULT_EVAL_CAPTURE_DIR
    return Path(raw).expanduser()


def append_eval_sample(
    *,
    output_dir: Path,
    audio_path: Path | None,
    raw_asr_text: str,
    final_text: str,
    record_backend: str,
    source: str,
    transcribe_latency_ms: float,
) -> dict[str, Any]:
    """Persist one (audio, raw ASR, final text) eval pair under ``output_dir``.

    Layout: ``<output_dir>/index.jsonl`` plus ``<output_dir>/audio/<id><suffix>``.
    The audio is copied at finalize time because the recorder's working file
    lives in a TemporaryDirectory that is deleted right after the utterance.

    Schema notes: ``gold`` starts False and ``edited_text`` starts as None.
    A later gold-marking pass (user's post-commit edits, e.g. surrounding-text
    diff) fills ``edited_text`` and flips ``gold`` to True; until then the
    ``final_text`` still contains any ASR errors the user did not fix, so it
    must not be treated as ground truth for CER.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except OSError:
        pass

    captured_at = datetime.now().astimezone()
    sample_id = f"{captured_at.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    audio_file: str | None = None
    audio_missing = True
    if audio_path is not None:
        source_path = Path(audio_path)
        if source_path.is_file():
            audio_subdir = output_dir / "audio"
            audio_subdir.mkdir(parents=True, exist_ok=True)
            suffix = source_path.suffix or ".wav"
            dest_name = f"{sample_id}{suffix}"
            shutil.copyfile(source_path, audio_subdir / dest_name)
            audio_file = f"audio/{dest_name}"
            audio_missing = False

    payload: dict[str, Any] = {
        "id": sample_id,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "audio_file": audio_file,
        "audio_missing": audio_missing,
        "raw_asr_text": str(raw_asr_text),
        "final_text": str(final_text),
        "record_backend": str(record_backend),
        "source": str(source),
        "transcribe_latency_ms": round(float(transcribe_latency_ms), 3),
        "gold": False,
        "edited_text": None,
        "user_edited": None,
        "notes": "",
    }
    with (output_dir / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload
