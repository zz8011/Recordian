from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
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


_GOLD_FALSE_RE = re.compile(rb"\"gold\"\s*:\s*false")

GOLD_MARKED = "marked"
GOLD_ALREADY = "already_gold"
GOLD_NO_MATCH = "no_match"


def mark_eval_sample_gold(*, output_dir: Path, final_text: str) -> str:
    """Flip ``gold`` to True on the newest capture row matching ``final_text``.

    The tray's confirm-last-text action vouches that the on-screen text is
    what the user actually said; that vouch is what promotes a captured row
    to a gold reference for the A/B harness (which then uses ``final_text``
    as the reference, preferring ``edited_text`` if a later correction pass
    fills it).

    Matching is deliberately strict: exact ``final_text`` equality, newest
    row first, and only a row whose ``gold`` is still False is flipped. A
    wrong gold is worse than none, so recency alone never suffices.

    Returns ``"marked"``, ``"already_gold"`` (an exact match exists but
    every match is already gold), or ``"no_match"`` (no row carries this
    ``final_text``, or the dataset does not exist). The rewrite changes only
    the flipped row's ``"gold": false`` token; every other line is carried
    over byte-for-byte via a temp file + rename in the same directory, and
    the index file's permissions are preserved.
    """
    index_path = output_dir / "index.jsonl"
    target = str(final_text)
    if not target or not index_path.is_file():
        return GOLD_NO_MATCH

    for _attempt in range(2):
        before = index_path.stat()
        segments = index_path.read_bytes().split(b"\n")

        saw_exact_match = False
        flip_at: int | None = None
        for position in range(len(segments) - 1, -1, -1):
            segment = segments[position]
            if not segment.strip():
                continue
            try:
                row = json.loads(segment)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("final_text", "")) != target:
                continue
            saw_exact_match = True
            if not row.get("gold"):
                flip_at = position
                break
        if flip_at is None:
            return GOLD_ALREADY if saw_exact_match else GOLD_NO_MATCH

        flipped, count = _GOLD_FALSE_RE.subn(b"\"gold\": true", segments[flip_at], count=1)
        if count == 0:
            raise ValueError("matched eval row has no flippable gold field")
        segments[flip_at] = flipped

        after = index_path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            continue  # a capture landed mid-rewrite; re-read and retry once

        fd, tmp_name = tempfile.mkstemp(dir=output_dir, prefix=".index-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"\n".join(segments))
            os.chmod(tmp_name, stat.S_IMODE(before.st_mode))
            os.replace(tmp_name, index_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return GOLD_MARKED
    raise RuntimeError("eval index kept changing while marking gold")
