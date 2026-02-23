from pathlib import Path

from recordian.audio import chunk_samples, read_wav_mono_f32, write_wav_mono_f32


def test_chunk_samples() -> None:
    samples = [0.0] * 1600
    chunks = chunk_samples(samples, sample_rate=16000, chunk_ms=100)
    assert len(chunks) == 1
    assert len(chunks[0]) == 1600


def test_wav_roundtrip(tmp_path: Path) -> None:
    wav_path = tmp_path / "test.wav"
    samples = [0.0, 0.1, -0.1, 0.3, -0.3]
    write_wav_mono_f32(wav_path, samples, sample_rate=16000)
    loaded = read_wav_mono_f32(wav_path, sample_rate=16000)

    assert len(loaded) == len(samples)
    # PCM16 round-trip has quantization error.
    assert abs(loaded[1] - samples[1]) < 0.01
