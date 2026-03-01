from __future__ import annotations

from pathlib import Path
import wave

import numpy as np

from recordian.speaker_verify import (
    SpeakerProfile,
    cosine_similarity,
    enroll_speaker_profile_from_wav,
    extract_speaker_embedding,
    load_speaker_profile,
    save_speaker_profile,
)


def _make_voice(duration_s: float, sample_rate: int, tones: tuple[float, float, float], noise: float = 0.01) -> np.ndarray:
    t = np.arange(int(duration_s * sample_rate), dtype=np.float32) / float(sample_rate)
    signal = (
        0.58 * np.sin(2 * np.pi * tones[0] * t)
        + 0.26 * np.sin(2 * np.pi * tones[1] * t)
        + 0.16 * np.sin(2 * np.pi * tones[2] * t)
    )
    rng = np.random.default_rng(42)
    signal = signal + rng.normal(0.0, noise, size=signal.shape).astype(np.float32)
    return np.clip(signal, -1.0, 1.0)


def _write_pcm16_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def test_extract_embedding_similarity_same_voice_higher_than_different() -> None:
    same_a = _make_voice(1.8, 16000, (180.0, 420.0, 760.0), noise=0.01)
    same_b = _make_voice(1.8, 16000, (182.0, 418.0, 758.0), noise=0.012)
    diff = _make_voice(1.8, 16000, (260.0, 540.0, 980.0), noise=0.01)

    emb_a = extract_speaker_embedding(same_a, sample_rate=16000)
    emb_b = extract_speaker_embedding(same_b, sample_rate=16000)
    emb_diff = extract_speaker_embedding(diff, sample_rate=16000)

    score_same = cosine_similarity(emb_a, emb_b)
    score_diff = cosine_similarity(emb_a, emb_diff)

    assert score_same > score_diff
    assert score_same > 0.7


def test_profile_save_load_roundtrip(tmp_path: Path) -> None:
    profile_path = tmp_path / "owner_profile.json"
    profile = SpeakerProfile(
        embedding=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        sample_rate=16000,
        created_at=123.456,
        source="unit-test",
    )
    save_speaker_profile(profile_path, profile)

    loaded = load_speaker_profile(profile_path)
    assert loaded is not None
    assert loaded.sample_rate == 16000
    assert loaded.source == "unit-test"
    assert len(loaded.embedding) == 8


def test_enroll_speaker_profile_from_wav_supports_non_16k_input(tmp_path: Path) -> None:
    sample_path = tmp_path / "owner.wav"
    profile_path = tmp_path / "owner_profile.json"
    signal = _make_voice(2.0, 22050, (200.0, 430.0, 820.0), noise=0.01)
    _write_pcm16_wav(sample_path, signal, sample_rate=22050)

    profile = enroll_speaker_profile_from_wav(
        sample_path=sample_path,
        profile_path=profile_path,
        target_rate=16000,
    )
    assert profile.sample_rate == 16000
    assert profile.source.endswith("owner.wav")
    assert len(profile.embedding) > 10

    loaded = load_speaker_profile(profile_path)
    assert loaded is not None
    assert len(loaded.embedding) == len(profile.embedding)
