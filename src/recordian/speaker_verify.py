from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any
import wave


@dataclass(slots=True)
class SpeakerProfile:
    embedding: list[float]
    sample_rate: int
    created_at: float
    source: str = ""
    feature_version: int = 2  # Version 2: with pre-emphasis


def _to_float32_mono(samples: Any):
    import numpy as np

    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    return data


def _resample_linear(samples, *, src_rate: int, dst_rate: int):
    if src_rate == dst_rate:
        return samples
    import numpy as np

    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if data.size == 0:
        return data
    out_len = max(1, int(round(data.size * float(dst_rate) / float(src_rate))))
    src_x = np.arange(data.size, dtype=np.float32)
    dst_x = np.linspace(0, data.size - 1, out_len, dtype=np.float32)
    return np.interp(dst_x, src_x, data).astype(np.float32)


def _apply_preemphasis(frame, coeff: float = 0.97):
    """Apply pre-emphasis filter to enhance high-frequency components.

    Formula: y[n] = x[n] - α*x[n-1], where α=0.97

    Args:
        frame: Audio frame (numpy array)
        coeff: Pre-emphasis coefficient (default 0.97)

    Returns:
        Pre-emphasized frame
    """
    import numpy as np

    if frame.size == 0:
        return frame

    emphasized = np.empty_like(frame)
    emphasized[0] = frame[0]
    emphasized[1:] = frame[1:] - coeff * frame[:-1]
    return emphasized


def extract_speaker_embedding(
    samples: Any,
    *,
    sample_rate: int,
    target_rate: int = 16000,
) -> list[float]:
    import numpy as np

    if sample_rate <= 0 or target_rate <= 0:
        raise ValueError("invalid_sample_rate")

    data = _to_float32_mono(samples)
    if data.size == 0:
        raise ValueError("empty_audio")

    peak = float(np.max(np.abs(data)))
    if peak > 1e-6:
        data = data / peak

    if sample_rate != target_rate:
        data = _resample_linear(data, src_rate=sample_rate, dst_rate=target_rate)

    min_samples = max(4800, int(target_rate * 0.30))
    if data.size < min_samples:
        raise ValueError("audio_too_short")

    frame_len = max(200, int(target_rate * 0.025))
    hop = max(80, int(target_rate * 0.010))
    n_fft = 512
    while n_fft < frame_len:
        n_fft *= 2
    window = np.hanning(frame_len).astype(np.float32)

    spectra = []
    for start in range(0, data.size - frame_len + 1, hop):
        frame = data[start : start + frame_len]
        rms = float(np.sqrt(np.mean(frame * frame)))
        if rms < 0.01:
            continue
        # Apply pre-emphasis to enhance high-frequency components
        frame = _apply_preemphasis(frame, coeff=0.97)
        spectrum = np.fft.rfft(frame * window, n=n_fft)
        power = np.abs(spectrum).astype(np.float32) ** 2
        spectra.append(np.log1p(power[1:]))  # drop DC

    if not spectra:
        raise ValueError("no_voiced_frame")

    spec = np.stack(spectra, axis=0)
    bands = np.array_split(spec, 24, axis=1)
    band_energy = np.stack([band.mean(axis=1) for band in bands], axis=1)

    voiced_ratio = float(len(spectra)) / max(1.0, float(data.size) / float(hop))
    feature = np.concatenate(
        [
            band_energy.mean(axis=0),
            band_energy.std(axis=0),
            np.array([voiced_ratio], dtype=np.float32),
        ],
        axis=0,
    ).astype(np.float32)

    norm = float(np.linalg.norm(feature))
    if norm <= 1e-8:
        raise ValueError("degenerate_embedding")

    feature = feature / norm
    return [float(v) for v in feature]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    import numpy as np

    a = np.asarray(left, dtype=np.float32).reshape(-1)
    b = np.asarray(right, dtype=np.float32).reshape(-1)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return -1.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-8:
        return -1.0
    score = float(np.dot(a, b) / denom)
    if score > 1.0:
        return 1.0
    if score < -1.0:
        return -1.0
    return score


def save_speaker_profile(path: Path, profile: SpeakerProfile) -> None:
    payload = {
        "version": 1,
        "sample_rate": int(profile.sample_rate),
        "created_at": float(profile.created_at),
        "source": str(profile.source),
        "embedding": [float(v) for v in profile.embedding],
        "feature_version": int(profile.feature_version),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_speaker_profile(path: Path) -> SpeakerProfile | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    embedding_raw = payload.get("embedding", [])
    if not isinstance(embedding_raw, list) or not embedding_raw:
        raise ValueError("invalid_profile_embedding")
    embedding = [float(v) for v in embedding_raw]
    if len(embedding) < 8:
        raise ValueError("profile_embedding_too_short")
    sample_rate = int(payload.get("sample_rate", 16000))
    created_at = float(payload.get("created_at", 0.0))
    source = str(payload.get("source", ""))
    feature_version = int(payload.get("feature_version", 1))  # Default to v1 for old profiles
    return SpeakerProfile(
        embedding=embedding,
        sample_rate=sample_rate,
        created_at=created_at,
        source=source,
        feature_version=feature_version,
    )


def _load_wav_any_f32(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_rate = int(wf.getframerate())
        sample_width = int(wf.getsampwidth())
        n_frames = int(wf.getnframes())
        payload = wf.readframes(n_frames)

    if channels < 1:
        raise ValueError("invalid_channels")

    if sample_width == 1:
        pcm = np.frombuffer(payload, dtype=np.uint8).astype(np.float32)
        pcm = (pcm - 128.0) / 128.0
    elif sample_width == 2:
        pcm = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        pcm = np.frombuffer(payload, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported_sample_width={sample_width}")

    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm.astype(np.float32), sample_rate


def enroll_speaker_profile_from_wav(
    *,
    sample_path: Path,
    profile_path: Path,
    target_rate: int = 16000,
) -> SpeakerProfile:
    samples, sample_rate = _load_wav_any_f32(sample_path)
    embedding = extract_speaker_embedding(samples, sample_rate=sample_rate, target_rate=target_rate)
    profile = SpeakerProfile(
        embedding=embedding,
        sample_rate=target_rate,
        created_at=time.time(),
        source=str(sample_path),
    )
    save_speaker_profile(profile_path, profile)
    return profile
