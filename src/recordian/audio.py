from __future__ import annotations

from array import array
from pathlib import Path
import sys
import wave


def read_wav_mono_f32(path: Path, *, sample_rate: int = 16000) -> list[float]:
    """Read PCM16 wav and return mono float samples in [-1, 1]."""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.getnframes()
        payload = wf.readframes(frames)

    if sampwidth != 2:
        raise ValueError(f"only PCM16 wav is supported, got sample width={sampwidth}")
    if rate != sample_rate:
        raise ValueError(f"unsupported sample rate={rate}, expected={sample_rate}")
    if channels < 1:
        raise ValueError("wav has invalid channel count")

    pcm = array("h")
    pcm.frombytes(payload)
    if sys.byteorder != "little":
        pcm.byteswap()

    if channels == 1:
        return [v / 32768.0 for v in pcm]

    mono: list[float] = []
    for i in range(0, len(pcm), channels):
        frame = pcm[i : i + channels]
        mono.append(sum(frame) / (channels * 32768.0))
    return mono


def chunk_samples(samples: list[float], *, sample_rate: int = 16000, chunk_ms: int = 480) -> list[list[float]]:
    stride = int(sample_rate * chunk_ms / 1000)
    if stride <= 0:
        raise ValueError("chunk_ms too small")

    if not samples:
        return []
    return [samples[i : i + stride] for i in range(0, len(samples), stride)]


def write_wav_mono_f32(path: Path, samples: list[float], *, sample_rate: int = 16000) -> None:
    pcm = array("h")
    for sample in samples:
        v = max(-1.0, min(1.0, sample))
        pcm.append(int(v * 32767.0))

    if sys.byteorder != "little":
        pcm.byteswap()

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
