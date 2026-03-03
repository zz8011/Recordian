from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SpeakerProfile:
    embedding: list[float]  # Deprecated: use embeddings for multi-sample profiles
    sample_rate: int
    created_at: float
    source: str = ""
    feature_version: int = 2  # Version 2: with pre-emphasis
    embeddings: list[list[float]] | None = None  # Multi-sample embeddings (v3+)

    def __post_init__(self):
        """Ensure embeddings is populated for consistency."""
        if self.embeddings is None:
            # Single-sample profile: wrap embedding in list
            self.embeddings = [self.embedding]
        elif not self.embeddings:
            # Empty embeddings: use single embedding
            self.embeddings = [self.embedding]


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
        coeff: Pre-emphasis coefficient (default 0.97, should be in [0, 1])

    Returns:
        Pre-emphasized frame
    """
    import numpy as np

    if frame.size == 0:
        return frame

    emphasized = np.empty_like(frame)
    emphasized[0] = frame[0]
    emphasized[1:] = frame[1:] - coeff * frame[:-1]
    # Clip to prevent numerical overflow in extreme cases
    return np.clip(emphasized, -2.0, 2.0)


def _assess_sample_quality(samples: Any, sample_rate: int) -> dict[str, float]:
    """Assess audio sample quality for speaker enrollment.

    Args:
        samples: Audio samples (numpy array or compatible)
        sample_rate: Sample rate in Hz

    Returns:
        Dictionary with quality metrics:
        - rms: Root mean square energy (before normalization)
        - voiced_ratio: Ratio of voiced frames (0.0-1.0)
        - duration_s: Duration in seconds
        - is_acceptable: Whether sample meets minimum quality
    """
    import numpy as np

    data = _to_float32_mono(samples)
    if data.size == 0:
        return {"rms": 0.0, "voiced_ratio": 0.0, "duration_s": 0.0, "is_acceptable": False}

    # Calculate RMS BEFORE normalization (to detect truly quiet samples)
    rms = float(np.sqrt(np.mean(data * data)))

    # Normalize for voiced ratio calculation
    peak = float(np.max(np.abs(data)))
    if peak > 1e-6:
        data = data / peak
    else:
        # Too quiet to be useful
        return {"rms": rms, "voiced_ratio": 0.0, "duration_s": len(data) / sample_rate, "is_acceptable": False}

    # Calculate voiced ratio using simple energy-based VAD
    frame_len = 400
    hop = 160
    voiced_frames = 0
    total_frames = 0

    for i in range(0, len(data) - frame_len, hop):
        frame = data[i : i + frame_len]
        frame_rms = float(np.sqrt(np.mean(frame * frame)))
        total_frames += 1
        if frame_rms > 0.01:  # Simple energy threshold
            voiced_frames += 1

    voiced_ratio = voiced_frames / total_frames if total_frames > 0 else 0.0
    duration_s = len(data) / sample_rate

    # Quality thresholds
    is_acceptable = rms > 0.005 and voiced_ratio > 0.3 and duration_s >= 0.5

    return {
        "rms": rms,
        "voiced_ratio": voiced_ratio,
        "duration_s": duration_s,
        "is_acceptable": is_acceptable,
    }


def _estimate_noise_floor(data: Any, sample_rate: int, window_s: float = 0.5) -> float:
    """Estimate noise floor from initial audio segment.

    Args:
        data: Normalized audio samples (float32, mono)
        sample_rate: Sample rate in Hz
        window_s: Duration of initial segment to analyze (default 0.5s)

    Returns:
        Estimated noise floor RMS value
    """
    import numpy as np

    window_samples = int(sample_rate * window_s)
    if data.size < window_samples:
        window_samples = data.size

    # Analyze initial segment
    initial_segment = data[:window_samples]

    # Calculate frame-level RMS
    frame_len = 400
    hop = 160
    rms_values = []

    for i in range(0, len(initial_segment) - frame_len, hop):
        frame = initial_segment[i : i + frame_len]
        frame_rms = float(np.sqrt(np.mean(frame * frame)))
        rms_values.append(frame_rms)

    if not rms_values:
        return 0.005  # Default fallback

    # Use 10th percentile as noise floor estimate (more conservative)
    noise_floor = float(np.percentile(rms_values, 10))
    return max(0.003, min(0.02, noise_floor))  # Clamp to reasonable range


def extract_speaker_embedding(
    samples: Any,
    *,
    sample_rate: int,
    target_rate: int = 16000,
    noise_suppression: int = 1,
) -> list[float]:
    """Extract speaker embedding from audio samples.

    Args:
        samples: Audio samples (numpy array or compatible)
        sample_rate: Sample rate in Hz
        target_rate: Target sample rate (default 16000)
        noise_suppression: Noise suppression level
            0 = disabled (fixed threshold 0.01)
            1 = standard (adaptive threshold, noise_floor * 2.5)
            2 = aggressive (adaptive threshold, noise_floor * 3.5)

    Returns:
        Normalized embedding vector (49 dimensions)
    """
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

    # Determine RMS threshold based on noise suppression level
    if noise_suppression == 0:
        rms_threshold = 0.01  # Fixed threshold (legacy behavior)
    else:
        noise_floor = _estimate_noise_floor(data, target_rate)
        if noise_suppression == 1:
            rms_threshold = noise_floor * 2.5  # Standard
        else:  # noise_suppression == 2
            rms_threshold = noise_floor * 3.5  # Aggressive

    frame_len = max(200, int(target_rate * 0.025))
    hop = max(80, int(target_rate * 0.010))
    n_fft = 512
    while n_fft < frame_len:
        n_fft *= 2
    window = np.hanning(frame_len).astype(np.float32)

    spectra = []
    total_frames = 0
    for start in range(0, data.size - frame_len + 1, hop):
        frame = data[start : start + frame_len]
        total_frames += 1
        rms = float(np.sqrt(np.mean(frame * frame)))
        if rms < rms_threshold:
            continue
        # Apply pre-emphasis to enhance high-frequency components
        frame = _apply_preemphasis(frame, coeff=0.97)
        spectrum = np.fft.rfft(frame * window, n=n_fft)
        power = np.abs(spectrum).astype(np.float32) ** 2
        spectra.append(np.log1p(power[1:]))  # drop DC

    # Ensure minimum 30% of frames are retained to avoid over-suppression
    min_frames = max(1, int(total_frames * 0.3))
    if len(spectra) < min_frames:
        # Fallback: re-extract with lower threshold
        spectra = []
        fallback_threshold = rms_threshold * 0.5
        for start in range(0, data.size - frame_len + 1, hop):
            frame = data[start : start + frame_len]
            rms = float(np.sqrt(np.mean(frame * frame)))
            if rms < fallback_threshold:
                continue
            frame = _apply_preemphasis(frame, coeff=0.97)
            spectrum = np.fft.rfft(frame * window, n=n_fft)
            power = np.abs(spectrum).astype(np.float32) ** 2
            spectra.append(np.log1p(power[1:]))

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
    # Save multi-sample embeddings if available
    if profile.embeddings and len(profile.embeddings) > 1:
        payload["embeddings"] = [[float(v) for v in emb] for emb in profile.embeddings]
    path.parent.mkdir(parents=True, exist_ok=True)

    # Set umask to ensure file is created with secure permissions (0o600)
    # This prevents TOCTOU vulnerability between file creation and chmod
    import os
    old_umask = os.umask(0o077)  # Ensure only owner can read/write
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # Explicitly set permissions (redundant but ensures correctness)
        try:
            path.chmod(0o600)
        except (OSError, NotImplementedError) as e:
            # Windows doesn't support Unix permissions, log warning but don't fail
            import logging
            logging.warning(f"Could not set secure permissions on {path}: {e}")
    finally:
        os.umask(old_umask)  # Restore original umask


def load_speaker_profile(path: Path) -> SpeakerProfile | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))

    # Load primary embedding (required for backward compatibility)
    embedding_raw = payload.get("embedding", [])
    if not isinstance(embedding_raw, list) or not embedding_raw:
        raise ValueError("invalid_profile_embedding")
    embedding = [float(v) for v in embedding_raw]
    if len(embedding) < 8:
        raise ValueError("profile_embedding_too_short")

    # Load multi-sample embeddings if available
    embeddings = None
    embeddings_raw = payload.get("embeddings", None)
    if embeddings_raw and isinstance(embeddings_raw, list):
        embeddings = [[float(v) for v in emb] for emb in embeddings_raw]
        # Validate all embeddings
        for emb in embeddings:
            if len(emb) < 8:
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
        embeddings=embeddings,
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


def enroll_speaker_profile_from_multiple_wavs(
    *,
    sample_paths: list[Path],
    profile_path: Path,
    target_rate: int = 16000,
    min_quality_rms: float = 0.005,
    min_quality_voiced_ratio: float = 0.3,
) -> SpeakerProfile:
    """Enroll speaker profile from multiple audio samples.

    Args:
        sample_paths: List of paths to WAV files (3-5 recommended)
        profile_path: Path to save the profile
        target_rate: Target sample rate (default 16000)
        min_quality_rms: Minimum RMS threshold for quality check
        min_quality_voiced_ratio: Minimum voiced ratio for quality check

    Returns:
        SpeakerProfile with multiple embeddings

    Raises:
        ValueError: If no samples pass quality check or list is empty
    """
    if not sample_paths:
        raise ValueError("sample_paths_empty")

    embeddings = []
    sources = []
    rejected_samples = []

    for sample_path in sample_paths:
        try:
            samples, sample_rate = _load_wav_any_f32(sample_path)

            # Assess quality
            quality = _assess_sample_quality(samples, sample_rate)
            if not quality["is_acceptable"]:
                rejected_samples.append(
                    {
                        "path": str(sample_path),
                        "reason": f"low_quality (rms={quality['rms']:.4f}, voiced={quality['voiced_ratio']:.2f})",
                    }
                )
                continue

            # Extract embedding
            embedding = extract_speaker_embedding(samples, sample_rate=sample_rate, target_rate=target_rate)
            embeddings.append(embedding)
            sources.append(str(sample_path))

        except Exception as e:
            rejected_samples.append({"path": str(sample_path), "reason": f"error: {type(e).__name__}"})

    if not embeddings:
        raise ValueError(f"no_valid_samples (rejected: {rejected_samples})")

    # Use first embedding as primary (for backward compatibility)
    primary_embedding = embeddings[0]
    source_summary = f"{len(embeddings)} samples: {', '.join(sources)}"

    profile = SpeakerProfile(
        embedding=primary_embedding,
        sample_rate=target_rate,
        created_at=time.time(),
        source=source_summary,
        embeddings=embeddings,
    )
    save_speaker_profile(profile_path, profile)
    return profile


def add_speaker_sample(
    *,
    profile_path: Path,
    sample_path: Path,
    min_quality_rms: float = 0.005,
    min_quality_voiced_ratio: float = 0.3,
) -> SpeakerProfile:
    """Add a new sample to an existing speaker profile.

    Args:
        profile_path: Path to existing profile
        sample_path: Path to new WAV sample
        min_quality_rms: Minimum RMS threshold
        min_quality_voiced_ratio: Minimum voiced ratio

    Returns:
        Updated SpeakerProfile

    Raises:
        ValueError: If profile doesn't exist or sample quality is too low
    """
    # Load existing profile
    profile = load_speaker_profile(profile_path)
    if profile is None:
        raise ValueError("profile_not_found")

    # Load and assess new sample
    samples, sample_rate = _load_wav_any_f32(sample_path)
    quality = _assess_sample_quality(samples, sample_rate)

    if not quality["is_acceptable"]:
        raise ValueError(
            f"sample_quality_too_low (rms={quality['rms']:.4f}, voiced={quality['voiced_ratio']:.2f})"
        )

    # Extract embedding
    embedding = extract_speaker_embedding(samples, sample_rate=sample_rate, target_rate=profile.sample_rate)

    # Add to embeddings list
    updated_embeddings = list(profile.embeddings) if profile.embeddings else [profile.embedding]
    updated_embeddings.append(embedding)

    # Update profile
    updated_profile = SpeakerProfile(
        embedding=profile.embedding,  # Keep original primary embedding
        sample_rate=profile.sample_rate,
        created_at=profile.created_at,
        source=f"{profile.source} + {sample_path}",
        feature_version=profile.feature_version,
        embeddings=updated_embeddings,
    )

    save_speaker_profile(profile_path, updated_profile)
    return updated_profile


