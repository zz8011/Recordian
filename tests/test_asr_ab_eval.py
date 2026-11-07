"""Tests for tools/asr_ab_eval.py chunking + partial-output behaviour.

No GPU needed: a fake provider stands in for the Confucius/Qwen providers.
"""
import importlib.util
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "asr_ab_eval.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("asr_ab_eval_under_test", TOOLS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_wav(path: Path, seconds: float, rate: int = 16000) -> None:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * frames)


class FakeProvider:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls: list[Path] = []

    def transcribe_file(self, wav_path, hotwords=None):
        self.calls.append(Path(wav_path))
        text = self.texts.pop(0) if self.texts else ""
        return SimpleNamespace(text=text)


def test_chunk_wav_splits_long_clip(tmp_path):
    mod = _load_harness()
    wav = tmp_path / "long.wav"
    _make_wav(wav, 50.0)
    parts = mod.chunk_wav(wav, 24.0, tmp_path)
    assert len(parts) == 3
    durations = []
    total_frames = 0
    for part in parts:
        with wave.open(str(part), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getframerate() == 16000
            durations.append(handle.getnframes() / handle.getframerate())
            total_frames += handle.getnframes()
    assert durations == pytest.approx([24.0, 24.0, 2.0])
    assert total_frames == 50 * 16000


def test_chunk_wav_short_clip_is_one_part(tmp_path):
    mod = _load_harness()
    wav = tmp_path / "short.wav"
    _make_wav(wav, 10.0)
    parts = mod.chunk_wav(wav, 24.0, tmp_path)
    assert len(parts) == 1
    with wave.open(str(parts[0]), "rb") as handle:
        assert handle.getnframes() / handle.getframerate() == pytest.approx(10.0)


def test_transcribe_all_chunks_long_clip_and_concatenates(tmp_path):
    mod = _load_harness()
    wav = tmp_path / "long.wav"
    _make_wav(wav, 50.0)
    provider = FakeProvider(["AA", "BB", "CC"])
    sink: list[dict] = []
    clips = [{"id": "long", "wav": wav, "reference": "AABBCC", "gold": False}]
    rows = mod.transcribe_all(provider, clips, chunk_s=24.0, sink=sink)
    assert len(provider.calls) == 3
    assert rows[0]["hypothesis"] == "AABBCC"
    assert rows[0]["duration_s"] == pytest.approx(50.0)
    assert rows[0]["cer"] == 0.0
    assert sink == rows


def test_transcribe_all_short_clip_is_single_session(tmp_path):
    mod = _load_harness()
    wav = tmp_path / "short.wav"
    _make_wav(wav, 10.0)
    provider = FakeProvider(["hello"])
    clips = [{"id": "short", "wav": wav, "reference": "hello", "gold": False}]
    rows = mod.transcribe_all(provider, clips, chunk_s=24.0)
    assert len(provider.calls) == 1
    assert rows[0]["hypothesis"] == "hello"


def test_build_payload_partial_marks_and_aggregates_subset():
    mod = _load_harness()
    clips = [
        {"id": "a", "wav": Path("a.wav"), "reference": "x", "gold": False},
        {"id": "b", "wav": Path("b.wav"), "reference": "y", "gold": False},
    ]
    completed = [
        {"id": "a", "ref_chars": 1, "errors": 0, "duration_s": 1.0,
         "elapsed_s": 1.0, "term_misses": [], "latin_ref_tokens": 0, "latin_survived": 0},
    ]
    payload = mod.build_payload("A", Path("/d"), clips, completed,
                                partial=True, error="RuntimeError: boom")
    assert payload["partial"] is True
    assert payload["error"] == "RuntimeError: boom"
    assert payload["aggregate"]["clips"] == 1
    assert payload["gold_references"] is False
    assert payload["gold_clips"] == 0
    full = mod.build_payload("A", Path("/d"), clips, completed)
    assert "partial" not in full and "error" not in full
