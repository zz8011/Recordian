from __future__ import annotations

import importlib.util
import json
import queue
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from recordian.eval_capture import append_eval_sample, mark_eval_sample_gold


def _append(output_dir: Path, final_text: str, raw: str | None = None) -> dict:
    return append_eval_sample(
        output_dir=output_dir,
        audio_path=None,
        raw_asr_text=raw if raw is not None else final_text,
        final_text=final_text,
        record_backend="ffmpeg-pulse",
        source="dictation",
        transcribe_latency_ms=1.0,
    )


def _index_bytes(output_dir: Path) -> bytes:
    return (output_dir / "index.jsonl").read_bytes()


def _rows(output_dir: Path) -> list[dict]:
    text = (output_dir / "index.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# -- mark_eval_sample_gold -------------------------------------------------


def test_mark_flips_most_recent_match_only(tmp_path: Path) -> None:
    output_dir = tmp_path / "eval-capture"
    _append(output_dir, "甲")
    _append(output_dir, "乙")
    _append(output_dir, "甲")
    before_lines = _index_bytes(output_dir).split(b"\n")

    assert mark_eval_sample_gold(output_dir=output_dir, final_text="甲") == "marked"

    rows = _rows(output_dir)
    assert [row["gold"] for row in rows] == [False, False, True]
    after_lines = _index_bytes(output_dir).split(b"\n")
    assert len(after_lines) == len(before_lines)
    for index, (before, after) in enumerate(zip(before_lines, after_lines, strict=True)):
        if index == 2:
            # the flipped line differs only in the gold token
            assert after.replace(b'"gold": true', b'"gold": false') == before
        else:
            assert after == before


def test_mark_second_call_flips_earlier_duplicate_then_reports_already(tmp_path: Path) -> None:
    output_dir = tmp_path / "eval-capture"
    _append(output_dir, "甲")
    _append(output_dir, "甲")

    assert mark_eval_sample_gold(output_dir=output_dir, final_text="甲") == "marked"
    assert mark_eval_sample_gold(output_dir=output_dir, final_text="甲") == "marked"
    assert [row["gold"] for row in _rows(output_dir)] == [True, True]

    before = _index_bytes(output_dir)
    assert mark_eval_sample_gold(output_dir=output_dir, final_text="甲") == "already_gold"
    assert _index_bytes(output_dir) == before  # no rewrite when nothing flips


def test_mark_no_match_leaves_file_untouched(tmp_path: Path) -> None:
    output_dir = tmp_path / "eval-capture"
    _append(output_dir, "最新的一句")
    before = _index_bytes(output_dir)

    # a different text never matches by recency alone
    assert mark_eval_sample_gold(output_dir=output_dir, final_text="别的内容") == "no_match"
    assert mark_eval_sample_gold(output_dir=output_dir, final_text="") == "no_match"
    assert _index_bytes(output_dir) == before
    assert _rows(output_dir)[0]["gold"] is False


def test_mark_missing_dataset_is_no_match(tmp_path: Path) -> None:
    assert mark_eval_sample_gold(output_dir=tmp_path / "gone", final_text="甲") == "no_match"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert mark_eval_sample_gold(output_dir=empty_dir, final_text="甲") == "no_match"


def test_mark_preserves_permissions_and_unrelated_content(tmp_path: Path) -> None:
    output_dir = tmp_path / "eval-capture"
    payload = _append(output_dir, "目标句")
    _append(output_dir, "别的句子")
    index = output_dir / "index.jsonl"
    # a row a later correction pass touched, plus a corrupt trailing line
    rows = _rows(output_dir)
    rows[0]["edited_text"] = "人工改定的文本"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    lines.append("{not json")
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    index.chmod(0o640)
    before_lines = index.read_bytes().split(b"\n")

    assert mark_eval_sample_gold(output_dir=output_dir, final_text="目标句") == "marked"

    assert (index.stat().st_mode & 0o777) == 0o640
    assert (output_dir.stat().st_mode & 0o777) == 0o700
    after_lines = index.read_bytes().split(b"\n")
    assert after_lines[1] == before_lines[1]  # untouched row, byte-for-byte
    assert after_lines[2] == b"{not json"  # corrupt line preserved verbatim
    marked = json.loads(after_lines[0])
    assert marked["gold"] is True
    assert marked["edited_text"] == "人工改定的文本"
    assert marked["id"] == payload["id"]


# -- tray confirm action ----------------------------------------------------


def _make_confirm_app(text: str, config: dict) -> SimpleNamespace:
    from recordian.tray_app import TrayApp

    app = TrayApp.__new__(TrayApp)
    app.state = SimpleNamespace(last_run=SimpleNamespace(text=text))
    app.events = queue.Queue()
    app._get_cached_config = lambda: config  # noqa: SLF001 - test seam
    return app


def _drain_messages(app: SimpleNamespace) -> list[str]:
    messages = []
    while not app.events.empty():
        messages.append(app.events.get()["message"])
    return messages


def _confirm_config(tmp_path: Path, dataset_dir: Path) -> dict:
    return {
        "auto_lexicon_db": str(tmp_path / "auto_lexicon.db"),
        "capture_eval_samples_path": str(dataset_dir),
    }


def test_tray_confirm_marks_gold_and_learns_lexicon(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "eval-capture"
    _append(dataset_dir, "Recordian 项目")
    app = _make_confirm_app("Recordian 项目", _confirm_config(tmp_path, dataset_dir))

    app.confirm_last_text_lexicon()

    assert _rows(dataset_dir)[0]["gold"] is True
    messages = _drain_messages(app)
    assert any("已确认上次文字并记入词库" in message for message in messages)
    assert any("标为金标" in message for message in messages)


def test_tray_confirm_gold_failure_does_not_change_lexicon_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recordian.auto_lexicon import AutoLexicon
    from recordian.tray_app import TrayApp

    def _boom(capture_dir: Path, text: str) -> str:
        raise RuntimeError("gold store unavailable")

    monkeypatch.setattr("recordian.tray_app.mark_last_text_gold", _boom)
    dataset_dir = tmp_path / "eval-capture"
    dataset_dir.mkdir()
    app = _make_confirm_app("Recordian 项目", _confirm_config(tmp_path, dataset_dir))

    TrayApp.confirm_last_text_lexicon(app)  # must not raise

    messages = _drain_messages(app)
    assert any("已确认上次文字并记入词库" in message for message in messages)
    assert any("标金标失败" in message for message in messages)
    lexicon = AutoLexicon(db_path=tmp_path / "auto_lexicon.db", min_accepts=2, max_terms=1000)
    try:
        info = lexicon.term_info("recordian")
        assert info is not None
        assert info["sources"]["user_confirmed"] == 1
    finally:
        lexicon.close()


def test_tray_confirm_without_matching_sample_reports_no_match(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "eval-capture"
    _append(dataset_dir, "别的一句")
    app = _make_confirm_app("Recordian 项目", _confirm_config(tmp_path, dataset_dir))

    app.confirm_last_text_lexicon()

    messages = _drain_messages(app)
    assert any("已确认上次文字并记入词库" in message for message in messages)
    assert any("未标金标" in message for message in messages)
    assert _rows(dataset_dir)[0]["gold"] is False


# -- A/B harness consumption -------------------------------------------------


def _load_harness():
    harness_path = Path(__file__).resolve().parents[1] / "tools" / "asr_ab_eval.py"
    spec = importlib.util.spec_from_file_location("asr_ab_eval", harness_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)


def _dataset_row(sample_id: str, audio_file: str, final_text: str, **overrides) -> dict:
    row = {
        "id": sample_id,
        "captured_at": "2026-11-08T00:00:00+08:00",
        "audio_file": audio_file,
        "audio_missing": False,
        "raw_asr_text": final_text,
        "final_text": final_text,
        "record_backend": "ffmpeg-pulse",
        "source": "dictation",
        "transcribe_latency_ms": 1.0,
        "gold": False,
        "edited_text": None,
        "user_edited": None,
        "notes": "",
    }
    row.update(overrides)
    return row


def test_harness_consumes_gold_marks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_harness()
    dataset_dir = tmp_path / "eval-capture"
    _write_wav(dataset_dir / "audio" / "a.wav")
    _write_wav(dataset_dir / "audio" / "b.wav")
    rows = [
        _dataset_row("a", "audio/a.wav", "plain final"),
        _dataset_row("b", "audio/b.wav", "confirmed final"),
    ]
    (dataset_dir / "index.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    # flip row b through the real marking path, exactly like the tray does
    assert mark_eval_sample_gold(output_dir=dataset_dir, final_text="confirmed final") == "marked"

    clips = harness.load_dataset(dataset_dir)
    assert [(clip["reference"], clip["gold"]) for clip in clips] == [
        ("plain final", False),
        ("confirmed final", True),
    ]

    class _StubProvider:
        def transcribe_file(self, wav: Path, hotwords: list) -> SimpleNamespace:
            return SimpleNamespace(text="stub hypothesis")

    results = harness.transcribe_all(_StubProvider(), clips)
    assert [row["gold"] for row in results] == [False, True]

    monkeypatch.setattr(harness, "load_context", lambda: "")
    monkeypatch.setattr(
        harness,
        "run_qwen_arm",
        lambda clips_, context: harness.transcribe_all(_StubProvider(), clips_),
    )
    out_path = tmp_path / "B.json"
    harness.cmd_run(SimpleNamespace(arm="B", dataset=str(dataset_dir), out=str(out_path)))
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["gold_clips"] == 1
    assert payload["gold_references"] is False  # not every clip is gold
    assert [clip["gold"] for clip in payload["clips"]] == [False, True]


def test_harness_prefers_edited_text_as_reference(tmp_path: Path) -> None:
    harness = _load_harness()
    dataset_dir = tmp_path / "eval-capture"
    _write_wav(dataset_dir / "audio" / "a.wav")
    row = _dataset_row("a", "audio/a.wav", "asr final", gold=True, edited_text="human corrected")
    (dataset_dir / "index.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    clips = harness.load_dataset(dataset_dir)
    assert clips[0]["reference"] == "human corrected"
    assert clips[0]["gold"] is True
