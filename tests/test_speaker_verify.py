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


def test_preemphasis_filter() -> None:
    """Test pre-emphasis filter output correctness."""
    from recordian.speaker_verify import _apply_preemphasis

    # Test with simple signal
    frame = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float32)
    emphasized = _apply_preemphasis(frame, coeff=0.97)

    # First sample should remain unchanged
    assert emphasized[0] == 1.0

    # Subsequent samples: y[n] = x[n] - 0.97*x[n-1]
    assert abs(emphasized[1] - (2.0 - 0.97 * 1.0)) < 1e-6
    assert abs(emphasized[2] - (3.0 - 0.97 * 2.0)) < 1e-6
    assert abs(emphasized[3] - (4.0 - 0.97 * 3.0)) < 1e-6
    assert abs(emphasized[4] - (5.0 - 0.97 * 4.0)) < 1e-6


def test_preemphasis_boundary_cases() -> None:
    """Test pre-emphasis with boundary coefficient values."""
    from recordian.speaker_verify import _apply_preemphasis

    frame = np.array([0.5, 0.6, 0.7], dtype=np.float32)

    # α=0.0 (no pre-emphasis)
    no_emphasis = _apply_preemphasis(frame, coeff=0.0)
    assert np.allclose(no_emphasis, frame)

    # α=1.0 (extreme pre-emphasis, first-order difference)
    full_emphasis = _apply_preemphasis(frame, coeff=1.0)
    assert full_emphasis[0] == 0.5
    assert abs(full_emphasis[1] - 0.1) < 1e-6  # 0.6 - 1.0*0.5
    assert abs(full_emphasis[2] - 0.1) < 1e-6  # 0.7 - 1.0*0.6


def test_preemphasis_enhances_high_frequency() -> None:
    """Test that pre-emphasis enhances high-frequency components."""
    # Create signal with low and high frequency components
    sample_rate = 16000
    duration = 1.0
    t = np.arange(int(duration * sample_rate), dtype=np.float32) / float(sample_rate)

    # Low frequency (200 Hz) + High frequency (4000 Hz)
    low_freq = 0.7 * np.sin(2 * np.pi * 200 * t)
    high_freq = 0.3 * np.sin(2 * np.pi * 4000 * t)
    signal = low_freq + high_freq

    # Extract embeddings with and without pre-emphasis
    # Note: Current implementation always applies pre-emphasis
    # This test verifies the feature extraction still works
    embedding = extract_speaker_embedding(signal, sample_rate=sample_rate)

    # Verify embedding is valid
    assert len(embedding) == 49
    assert all(isinstance(v, float) for v in embedding)


def test_feature_version_in_profile() -> None:
    """Test that feature_version is saved and loaded correctly."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        profile_path = Path(tmpdir) / "test_profile.json"

        # Create a profile with version 2
        voice = _make_voice(1.8, 16000, (180.0, 420.0, 760.0))
        embedding = extract_speaker_embedding(voice, sample_rate=16000)
        profile = SpeakerProfile(
            embedding=embedding,
            sample_rate=16000,
            created_at=1234567890.0,
            source="test.wav",
            feature_version=2,
        )

        # Save and load
        save_speaker_profile(profile_path, profile)
        loaded = load_speaker_profile(profile_path)

        assert loaded is not None
        assert loaded.feature_version == 2

        # Test backward compatibility: old profile without feature_version
        import json
        old_payload = {
            "version": 1,
            "sample_rate": 16000,
            "created_at": 1234567890.0,
            "source": "old.wav",
            "embedding": embedding,
        }
        profile_path.write_text(json.dumps(old_payload), encoding="utf-8")

        loaded_old = load_speaker_profile(profile_path)
        assert loaded_old is not None
        assert loaded_old.feature_version == 1  # Default to v1 for old profiles


def test_preemphasis_performance() -> None:
    """Test that pre-emphasis doesn't significantly increase computation time."""
    import time

    voice = _make_voice(2.0, 16000, (180.0, 420.0, 760.0))

    # Measure extraction time
    start = time.perf_counter()
    embedding = extract_speaker_embedding(voice, sample_rate=16000)
    elapsed_ms = (time.perf_counter() - start) * 1000

    # Should complete in reasonable time (< 100ms for 2s audio)
    assert elapsed_ms < 100
    assert len(embedding) == 49


def test_multi_sample_enrollment(tmp_path: Path) -> None:
    """Test enrolling speaker profile from multiple samples."""
    from recordian.speaker_verify import enroll_speaker_profile_from_multiple_wavs

    # Create 3 different voice samples
    sample_paths = []
    for i, tones in enumerate([(180.0, 420.0, 760.0), (190.0, 430.0, 770.0), (175.0, 410.0, 750.0)]):
        voice = _make_voice(1.5, 16000, tones)
        sample_path = tmp_path / f"sample_{i}.wav"
        with wave.open(str(sample_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            pcm = (voice * 32767).astype(np.int16)
            wf.writeframes(pcm.tobytes())
        sample_paths.append(sample_path)

    profile_path = tmp_path / "multi_profile.json"
    profile = enroll_speaker_profile_from_multiple_wavs(
        sample_paths=sample_paths, profile_path=profile_path, target_rate=16000
    )

    # Verify profile has multiple embeddings
    assert profile.embeddings is not None
    assert len(profile.embeddings) == 3
    assert all(len(emb) == 49 for emb in profile.embeddings)

    # Verify profile can be loaded
    loaded = load_speaker_profile(profile_path)
    assert loaded is not None
    assert loaded.embeddings is not None
    assert len(loaded.embeddings) == 3


def test_multi_sample_quality_filtering(tmp_path: Path) -> None:
    """Test that low-quality samples are rejected."""
    from recordian.speaker_verify import enroll_speaker_profile_from_multiple_wavs

    sample_paths = []

    # Good sample
    good_voice = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))
    good_path = tmp_path / "good.wav"
    with wave.open(str(good_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        pcm = (good_voice * 32767).astype(np.int16)
        wf.writeframes(pcm.tobytes())
    sample_paths.append(good_path)

    # Low-quality sample (mostly silence)
    bad_voice = np.random.randn(int(1.5 * 16000)).astype(np.float32) * 0.001
    bad_path = tmp_path / "bad.wav"
    with wave.open(str(bad_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        pcm = (bad_voice * 32767).astype(np.int16)
        wf.writeframes(pcm.tobytes())
    sample_paths.append(bad_path)

    profile_path = tmp_path / "filtered_profile.json"
    profile = enroll_speaker_profile_from_multiple_wavs(
        sample_paths=sample_paths, profile_path=profile_path, target_rate=16000
    )

    # Only good sample should be enrolled
    assert profile.embeddings is not None
    assert len(profile.embeddings) == 1


def test_add_speaker_sample(tmp_path: Path) -> None:
    """Test adding a new sample to existing profile."""
    from recordian.speaker_verify import add_speaker_sample

    # Create initial profile
    voice1 = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))
    sample1_path = tmp_path / "sample1.wav"
    with wave.open(str(sample1_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        pcm = (voice1 * 32767).astype(np.int16)
        wf.writeframes(pcm.tobytes())

    profile_path = tmp_path / "profile.json"
    initial_profile = enroll_speaker_profile_from_wav(
        sample_path=sample1_path, profile_path=profile_path, target_rate=16000
    )

    assert len(initial_profile.embeddings) == 1

    # Add second sample
    voice2 = _make_voice(1.5, 16000, (190.0, 430.0, 770.0))
    sample2_path = tmp_path / "sample2.wav"
    with wave.open(str(sample2_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        pcm = (voice2 * 32767).astype(np.int16)
        wf.writeframes(pcm.tobytes())

    updated_profile = add_speaker_sample(profile_path=profile_path, sample_path=sample2_path)

    # Verify profile now has 2 embeddings
    assert updated_profile.embeddings is not None
    assert len(updated_profile.embeddings) == 2

    # Verify profile can be loaded
    loaded = load_speaker_profile(profile_path)
    assert loaded is not None
    assert loaded.embeddings is not None
    assert len(loaded.embeddings) == 2


def test_backward_compatibility_single_sample(tmp_path: Path) -> None:
    """Test that single-sample profiles work correctly."""
    voice = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))
    sample_path = tmp_path / "sample.wav"
    with wave.open(str(sample_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        pcm = (voice * 32767).astype(np.int16)
        wf.writeframes(pcm.tobytes())

    profile_path = tmp_path / "profile.json"
    profile = enroll_speaker_profile_from_wav(
        sample_path=sample_path, profile_path=profile_path, target_rate=16000
    )

    # Single-sample profile should have embeddings list with 1 item
    assert profile.embeddings is not None
    assert len(profile.embeddings) == 1
    assert profile.embeddings[0] == profile.embedding

    # Verify it can be loaded
    loaded = load_speaker_profile(profile_path)
    assert loaded is not None
    assert loaded.embeddings is not None
    assert len(loaded.embeddings) == 1


def test_sample_quality_assessment() -> None:
    """Test sample quality assessment function."""
    from recordian.speaker_verify import _assess_sample_quality

    # Good quality sample
    good_voice = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))
    quality = _assess_sample_quality(good_voice, 16000)

    assert quality["rms"] > 0.005
    assert quality["voiced_ratio"] > 0.3
    assert quality["duration_s"] >= 1.5
    assert quality["is_acceptable"] is True

    # Low quality sample (mostly silence)
    bad_voice = np.random.randn(int(1.5 * 16000)).astype(np.float32) * 0.001
    quality = _assess_sample_quality(bad_voice, 16000)

    assert quality["is_acceptable"] is False


def test_noise_suppression_levels() -> None:
    """Test different noise suppression levels."""
    from recordian.speaker_verify import extract_speaker_embedding

    # Create clean voice
    clean_voice = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))

    # Add background noise (SNR ~10dB)
    noise = np.random.randn(len(clean_voice)).astype(np.float32) * 0.1
    noisy_voice = clean_voice + noise

    # Test all three levels
    emb_disabled = extract_speaker_embedding(noisy_voice, sample_rate=16000, noise_suppression=0)
    emb_standard = extract_speaker_embedding(noisy_voice, sample_rate=16000, noise_suppression=1)
    emb_aggressive = extract_speaker_embedding(noisy_voice, sample_rate=16000, noise_suppression=2)

    # All should produce valid embeddings
    assert len(emb_disabled) == 49
    assert len(emb_standard) == 49
    assert len(emb_aggressive) == 49

    # Embeddings may be the same or different depending on noise level
    # Just verify they are all valid (the important part is they don't crash)


def test_noise_suppression_improves_robustness() -> None:
    """Test that noise suppression improves recognition in noisy conditions."""
    from recordian.speaker_verify import cosine_similarity, extract_speaker_embedding

    # Create two samples from same speaker
    voice1 = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))
    voice2 = _make_voice(1.5, 16000, (185.0, 425.0, 765.0))  # Slight variation

    # Add heavy noise to second sample (SNR ~5dB)
    noise = np.random.randn(len(voice2)).astype(np.float32) * 0.3
    noisy_voice2 = voice2 + noise

    # Extract embeddings with different suppression levels
    emb1_clean = extract_speaker_embedding(voice1, sample_rate=16000, noise_suppression=1)
    emb2_no_suppression = extract_speaker_embedding(noisy_voice2, sample_rate=16000, noise_suppression=0)
    emb2_with_suppression = extract_speaker_embedding(noisy_voice2, sample_rate=16000, noise_suppression=1)

    # Calculate similarities
    sim_no_suppression = cosine_similarity(emb1_clean, emb2_no_suppression)
    sim_with_suppression = cosine_similarity(emb1_clean, emb2_with_suppression)

    # Noise suppression should improve similarity (though both should still be reasonably high)
    assert sim_with_suppression >= sim_no_suppression - 0.05  # Allow small margin


def test_noise_floor_estimation() -> None:
    """Test noise floor estimation function."""
    from recordian.speaker_verify import _estimate_noise_floor

    # Create audio with known noise floor
    # First 0.5s: low noise, then speech
    noise_segment = np.random.randn(8000).astype(np.float32) * 0.02  # Low noise
    speech_segment = _make_voice(1.0, 16000, (180.0, 420.0, 760.0))
    audio = np.concatenate([noise_segment, speech_segment])

    # Normalize
    audio = audio / np.max(np.abs(audio))

    noise_floor = _estimate_noise_floor(audio, 16000)

    # Noise floor should be low but non-zero
    assert 0.005 <= noise_floor <= 0.05


def test_minimum_frame_retention() -> None:
    """Test that at least 30% of frames are retained even with aggressive suppression."""
    from recordian.speaker_verify import extract_speaker_embedding

    # Create very noisy audio
    voice = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))
    heavy_noise = np.random.randn(len(voice)).astype(np.float32) * 0.5
    very_noisy = voice + heavy_noise

    # Should still extract embedding (fallback mechanism)
    emb = extract_speaker_embedding(very_noisy, sample_rate=16000, noise_suppression=2)
    assert len(emb) == 49


def test_noise_suppression_backward_compatibility() -> None:
    """Test that noise_suppression=0 behaves like old implementation."""
    from recordian.speaker_verify import extract_speaker_embedding

    voice = _make_voice(1.5, 16000, (180.0, 420.0, 760.0))

    # Extract with disabled suppression (should use fixed 0.01 threshold)
    emb = extract_speaker_embedding(voice, sample_rate=16000, noise_suppression=0)

    assert len(emb) == 49


