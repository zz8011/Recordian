from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_REFINE_CAPTURE_PATH = "~/.local/share/recordian/refine-samples.jsonl"


def resolve_refine_capture_path(value: object) -> Path:
    raw = str(value).strip() or DEFAULT_REFINE_CAPTURE_PATH
    return Path(raw).expanduser()


def append_refine_sample(
    *,
    output_path: Path,
    audio_path: Path,
    raw_asr_text: str,
    final_text: str,
    refine_applied: bool,
    refine_changed: bool,
    refine_preset: str,
    refine_provider: str,
    refine_model: str,
    refine_enabled: bool | None = None,
    refiner_ready: bool | None = None,
    record_backend: str,
    transcribe_latency_ms: float,
    refine_latency_ms: float,
    commit_info: dict[str, object],
) -> None:
    payload: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audio_path": str(audio_path),
        "raw_asr_text": str(raw_asr_text),
        "final_text": str(final_text),
        "refine_applied": bool(refine_applied),
        "refine_changed": bool(refine_changed),
        "refine_preset": str(refine_preset),
        "refine_provider": str(refine_provider),
        "refine_model": str(refine_model),
        "record_backend": str(record_backend),
        "transcribe_latency_ms": round(float(transcribe_latency_ms), 3),
        "refine_latency_ms": round(float(refine_latency_ms), 3),
        "commit": {
            "backend": str(commit_info.get("backend", "")),
            "committed": bool(commit_info.get("committed", False)),
            "detail": str(commit_info.get("detail", "")),
        },
    }
    if refine_enabled is not None:
        payload["refine_enabled"] = bool(refine_enabled)
    if refiner_ready is not None:
        payload["refiner_ready"] = bool(refiner_ready)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
