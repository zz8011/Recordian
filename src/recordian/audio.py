from __future__ import annotations

from pathlib import Path
import sys
import wave

import numpy as np


def read_wav_mono_f32(path: Path, *, sample_rate: int = 16000) -> np.ndarray:
    """读取 PCM16 WAV，返回 mono float32 numpy 数组，值域 [-1, 1]"""
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

    pcm = np.frombuffer(payload, dtype="<i2")  # little-endian int16

    if channels == 1:
        return pcm.astype(np.float32) / 32768.0

    pcm = pcm.reshape(-1, channels)
    return pcm.mean(axis=1).astype(np.float32) / 32768.0


def chunk_samples(
    samples: np.ndarray, *, sample_rate: int = 16000, chunk_ms: int = 480
) -> list[np.ndarray]:
    """将音频样本分块"""
    stride = int(sample_rate * chunk_ms / 1000)
    if stride <= 0:
        raise ValueError("chunk_ms too small")
    if len(samples) == 0:
        return []
    return [samples[i : i + stride] for i in range(0, len(samples), stride)]


def write_wav_mono_f32(
    path: Path, samples: np.ndarray, *, sample_rate: int = 16000
) -> None:
    """将 float32 numpy 数组写入 PCM16 WAV"""
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype(np.int16)
    if sys.byteorder != "little":
        pcm = pcm.byteswap()

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
