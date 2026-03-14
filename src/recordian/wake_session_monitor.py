from __future__ import annotations

import argparse
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .audio import write_wav_mono_f32

EventCallback = Callable[[dict[str, object]], None]
StateGetter = Callable[[str], object]
StateSetter = Callable[[str, object], None]


def _pick_vad_sample_rate(sample_rate: int) -> int:
    if sample_rate in {8000, 16000, 32000, 48000}:
        return sample_rate
    return 16000


def _vad_frame_bytes(sample_rate: int, frame_ms: int) -> int:
    samples = sample_rate * frame_ms // 1000
    return samples * 2


def _float_to_pcm16le(samples: Any) -> bytes:
    import numpy as np

    data = np.asarray(samples, dtype=np.float32)
    if data.size == 0:
        return b""
    clipped = np.clip(data, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    return pcm.tobytes()


def _resample_audio_for_vad(samples: Any, *, src_rate: int, dst_rate: int) -> Any:
    if src_rate == dst_rate:
        return samples
    import numpy as np

    data = np.asarray(samples, dtype=np.float32)
    if data.size == 0:
        return data

    out_len = max(1, int(round(data.size * dst_rate / src_rate)))
    src_x = np.arange(data.size, dtype=np.float32)
    dst_x = np.linspace(0, data.size - 1, out_len, dtype=np.float32)
    return np.interp(dst_x, src_x, data).astype(np.float32)


def _adaptive_vad_threshold(base: float, noise_level: float) -> float:
    """根据环境噪声动态调整 VAD 阈值，下限为 base * 0.4，上限为 base。"""
    min_thresh = base * 0.4
    if noise_level <= 0.0:
        return min_thresh
    if noise_level >= base:
        return base
    ratio = noise_level / base
    return min_thresh + (base - min_thresh) * ratio


def _is_level_speech_frame(*, level: float, rms: float, noise_floor: float) -> bool:
    """Level fallback: require both normalized level and RMS above adaptive noise-aware thresholds."""
    floor = max(0.0, noise_floor)
    dynamic_level_threshold = _adaptive_vad_threshold(0.08, floor * 20.0)
    dynamic_rms_threshold = max(0.0025, floor * 2.2)
    return level >= dynamic_level_threshold and rms >= dynamic_rms_threshold


def _update_speech_evidence(
    score_s: float,
    *,
    speech_detected_raw: bool,
    frame_duration_s: float,
    confirm_s: float,
) -> tuple[float, bool]:
    """Smooth raw frame-level speech flags to avoid start/stop jitter from transient spikes."""
    evidence = max(0.0, float(score_s))
    dt = max(0.0, float(frame_duration_s))
    threshold = max(0.0, float(confirm_s))
    if speech_detected_raw:
        cap = max(threshold * 3.0, dt)
        evidence = min(cap, evidence + dt)
    else:
        evidence = max(0.0, evidence - dt * 1.6)
    if threshold == 0.0:
        return evidence, bool(speech_detected_raw)
    return evidence, evidence >= threshold


def _owner_gate_level(level: float, *, owner_filter_enabled: bool, owner_active: bool) -> float:
    value = min(1.0, max(0.0, float(level)))
    if not owner_filter_enabled:
        return value
    if owner_active:
        return value
    return 0.0


def _semantic_text_signal_len(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"))


def _semantic_text_has_content(text: str, *, min_chars: int) -> bool:
    return _semantic_text_signal_len(text) >= max(1, int(min_chars))


def _semantic_probe_text(
    *,
    provider: Any,
    samples: list[float],
    sample_rate: int,
    hotwords: list[str],
    timeout_ms: int,
    normalize_final_text: Callable[[str], str] | None = None,
) -> str:
    if not samples:
        return ""
    with TemporaryDirectory(prefix="recordian-semantic-probe-") as temp_dir:
        wav_path = Path(temp_dir) / "probe.wav"
        write_wav_mono_f32(wav_path, samples, sample_rate=sample_rate)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(provider.transcribe_file, wav_path, hotwords=hotwords)
            try:
                result = future.result(timeout=max(0.2, timeout_ms / 1000.0))
            except TimeoutError:
                future.cancel()
                return ""
            except Exception:  # noqa: BLE001
                return ""
        text = getattr(result, "text", "")
        if normalize_final_text is not None:
            return normalize_final_text(str(text))
        return str(text).strip()


def _should_auto_stop_semantic_session(
    *,
    now_ts: float,
    started_ts: float,
    last_speech_ts: float,
    semantic_has_text: bool,
    semantic_last_text_ts: float,
    no_speech_timeout_s: float,
    min_speech_s: float,
    semantic_end_silence_s: float,
    acoustic_silence_s: float,
) -> str | None:
    """Return semantic auto-stop reason, or None when the session should continue."""
    now = float(now_ts)
    started = float(started_ts)
    last_speech = float(last_speech_ts)
    no_speech_timeout = max(0.0, float(no_speech_timeout_s))
    min_speech = max(0.0, float(min_speech_s))
    semantic_end_silence = max(0.2, float(semantic_end_silence_s))
    acoustic_silence = max(0.0, float(acoustic_silence_s))

    if not semantic_has_text:
        inactivity_base = max(started, last_speech)
        if no_speech_timeout > 0 and now - inactivity_base >= no_speech_timeout:
            return "semantic_no_text_timeout"
        return None

    if now - started < min_speech:
        return None
    if now - float(semantic_last_text_ts) < semantic_end_silence:
        return None
    if now - last_speech < acoustic_silence:
        return None
    return "semantic_silence"


def _pcm16le_to_f32(data: bytes, *, channels: int = 1) -> list:
    """将 PCM 16-bit LE 字节转换为 float32 单声道列表（多声道取平均）。"""
    import struct

    n_samples = len(data) // 2
    samples = struct.unpack(f"<{n_samples}h", data)
    frames = n_samples // channels
    result = []
    for i in range(frames):
        frame = samples[i * channels : (i + 1) * channels]
        avg = sum(frame) / channels / 32768.0
        result.append(avg)
    return result


@dataclass(slots=True)
class WakeSessionMonitorContext:
    args: argparse.Namespace
    record_handle: Any
    provider: Any
    stop_event: threading.Event
    get_state: StateGetter
    set_state: StateSetter
    resolve_hotwords: Callable[[], list[str]]
    stop_recording: Callable[[], bool]
    normalize_final_text: Callable[[str], str]
    on_state: EventCallback


def start_wake_session_monitor(context: WakeSessionMonitorContext) -> threading.Thread:
    def _level_worker() -> None:
        try:
            import numpy as np

            noise_floor = 0.0015
            smoothed_level = 0.0
            monitor_stream = getattr(context.record_handle, "monitor_stream", None)
            sample_rate = int(getattr(context.record_handle, "monitor_sample_rate", 16000))
            monitor_channels = max(1, int(getattr(context.record_handle, "monitor_channels", 1)))
            device_id = None

            if monitor_stream is None:
                import sounddevice as sd

                device_name = context.args.input_device if context.args.input_device != "default" else None
                try:
                    if device_name:
                        devices = sd.query_devices()
                        for i, dev in enumerate(devices):
                            if device_name in dev["name"] and dev["max_input_channels"] > 0:
                                device_id = i
                                break
                        if device_id is None:
                            device_id = device_name

                    device_info = sd.query_devices(device_id, kind="input")
                    sample_rate = int(device_info["default_samplerate"])
                    if context.args.debug_diagnostics:
                        context.on_state(
                            {
                                "event": "log",
                                "message": (
                                    "diag audio_level_monitoring_fallback"
                                    f" device={device_info['name']}"
                                    f" samplerate={sample_rate}"
                                ),
                            }
                        )
                except Exception:
                    device_id = None
                    sample_rate = 16000
            elif context.args.debug_diagnostics:
                context.on_state(
                    {
                        "event": "log",
                        "message": (
                            "diag audio_level_monitoring source=record_monitor"
                            f" samplerate={sample_rate}"
                            f" channels={monitor_channels}"
                        ),
                    }
                )

            use_webrtc_vad = bool(getattr(context.args, "wake_use_webrtcvad", True))
            try:
                vad_aggressiveness = int(getattr(context.args, "wake_vad_aggressiveness", 2))
            except Exception:
                vad_aggressiveness = 2
            if vad_aggressiveness not in {0, 1, 2, 3}:
                vad_aggressiveness = 2
            try:
                vad_frame_ms = int(getattr(context.args, "wake_vad_frame_ms", 30))
            except Exception:
                vad_frame_ms = 30
            if vad_frame_ms not in {10, 20, 30}:
                vad_frame_ms = 30
            vad_sample_rate = _pick_vad_sample_rate(sample_rate)
            vad_frame_bytes = _vad_frame_bytes(vad_sample_rate, vad_frame_ms)
            vad_pcm_buffer = bytearray()
            vad = None
            vad_init_attempted = False
            vad_log_emitted = False
            try:
                wake_speech_confirm_s = max(0.0, float(getattr(context.args, "wake_speech_confirm_s", 0.18)))
            except Exception:
                wake_speech_confirm_s = 0.18
            speech_evidence_s = 0.0
            semantic_enabled = bool(context.get_state("voice_semantic_enabled"))
            try:
                semantic_probe_interval_s = max(0.1, float(getattr(context.args, "wake_semantic_probe_interval_s", 0.45)))
            except Exception:
                semantic_probe_interval_s = 0.45
            try:
                semantic_window_s = max(0.4, float(getattr(context.args, "wake_semantic_window_s", 1.2)))
            except Exception:
                semantic_window_s = 1.2
            try:
                semantic_end_silence_s = max(0.2, float(getattr(context.args, "wake_semantic_end_silence_s", 1.0)))
            except Exception:
                semantic_end_silence_s = 1.0
            try:
                semantic_min_chars = max(1, int(getattr(context.args, "wake_semantic_min_chars", 1)))
            except Exception:
                semantic_min_chars = 1
            try:
                semantic_timeout_ms = max(200, int(getattr(context.args, "wake_semantic_timeout_ms", 1200)))
            except Exception:
                semantic_timeout_ms = 1200
            semantic_ring_s = max(semantic_window_s * 1.5, 2.0)
            semantic_max_samples = max(1, int(sample_rate * semantic_ring_s))
            semantic_window_samples = max(1, int(sample_rate * semantic_window_s))
            semantic_buffer: list[float] = []
            semantic_lock = threading.Lock()
            owner_filter_enabled = bool(context.get_state("voice_owner_filter_enabled"))
            owner_threshold = min(0.99, max(0.0, float(getattr(context.args, "wake_owner_threshold", 0.72))))
            owner_window_s = max(0.6, float(getattr(context.args, "wake_owner_window_s", 1.6)))
            owner_deactivate_margin = 0.06
            owner_activate_confirm = 1
            owner_deactivate_confirm = 2
            owner_pass_streak = 0
            owner_fail_streak = 0
            owner_verify_interval_s = 0.35
            owner_min_samples = max(3200, int(sample_rate * 0.35))
            owner_max_samples = max(1, int(sample_rate * owner_window_s))
            owner_audio_chunks: Any = None
            owner_audio_samples = 0
            owner_last_verify_ts = 0.0
            owner_last_active = False
            owner_embeddings: list[list[float]] | None = None
            _extract_owner_embedding = None
            _owner_cosine_similarity = None

            if owner_filter_enabled:
                try:
                    from collections import deque

                    from .speaker_verify import (
                        cosine_similarity as _cosine_similarity,
                    )
                    from .speaker_verify import (
                        enroll_speaker_profile_from_wav,
                        load_speaker_profile,
                    )
                    from .speaker_verify import (
                        extract_speaker_embedding as _extract_speaker_embedding,
                    )

                    profile_path = Path(
                        str(getattr(context.args, "wake_owner_profile", "~/.config/recordian/owner_voice_profile.json"))
                    ).expanduser()
                    sample_path_raw = str(getattr(context.args, "wake_owner_sample", "")).strip()
                    sample_path = Path(sample_path_raw).expanduser() if sample_path_raw else None
                    profile = load_speaker_profile(profile_path)
                    if profile is None and sample_path is not None and sample_path.exists():
                        profile = enroll_speaker_profile_from_wav(
                            sample_path=sample_path,
                            profile_path=profile_path,
                            target_rate=sample_rate,
                        )
                        context.on_state({"event": "log", "message": f"voice_owner_profile_enrolled: {profile_path}"})
                    if profile is None:
                        owner_filter_enabled = False
                        context.on_state({"event": "log", "message": "voice_owner_filter_disabled: profile_not_found"})
                    elif int(getattr(profile, "feature_version", 1)) != 2:
                        old_profile_version = int(getattr(profile, "feature_version", 1))
                        if sample_path is not None and sample_path.exists():
                            profile = enroll_speaker_profile_from_wav(
                                sample_path=sample_path,
                                profile_path=profile_path,
                                target_rate=sample_rate,
                            )
                            context.on_state(
                                {
                                    "event": "log",
                                    "message": (
                                        "voice_owner_profile_reenrolled: "
                                        f"{profile_path} (old_version={old_profile_version})"
                                    ),
                                }
                            )
                            owner_embeddings = list(profile.embeddings) if profile.embeddings else [list(profile.embedding)]
                            _extract_owner_embedding = _extract_speaker_embedding
                            _owner_cosine_similarity = _cosine_similarity
                            owner_audio_chunks = deque(maxlen=100)
                            if context.args.debug_diagnostics:
                                context.on_state(
                                    {
                                        "event": "log",
                                        "message": (
                                            "diag voice_owner_filter enabled"
                                            f" threshold={owner_threshold:.2f}"
                                            f" window_s={owner_window_s:.2f}"
                                        ),
                                    }
                                )
                        else:
                            owner_filter_enabled = False
                            context.on_state(
                                {
                                    "event": "log",
                                    "message": (
                                        "voice_owner_filter_disabled: profile_version_mismatch "
                                        f"(expected=2, got={old_profile_version}). "
                                        "Please re-enroll owner profile with current version."
                                    ),
                                }
                            )
                    else:
                        owner_embeddings = list(profile.embeddings) if profile.embeddings else [list(profile.embedding)]
                        _extract_owner_embedding = _extract_speaker_embedding
                        _owner_cosine_similarity = _cosine_similarity
                        owner_audio_chunks = deque(maxlen=100)
                        if context.args.debug_diagnostics:
                            context.on_state(
                                {
                                    "event": "log",
                                    "message": (
                                        "diag voice_owner_filter enabled"
                                        f" threshold={owner_threshold:.2f}"
                                        f" window_s={owner_window_s:.2f}"
                                    ),
                                }
                            )
                except Exception as exc:  # noqa: BLE001
                    owner_filter_enabled = False
                    context.on_state({"event": "log", "message": f"voice_owner_filter_disabled: {type(exc).__name__}: {exc}"})

            context.set_state("voice_owner_filter_enabled", owner_filter_enabled)
            # Keep initial speech responsive after wake trigger. The owner gate
            # will flip this to False after an explicit non-owner decision.
            context.set_state("voice_owner_active", True)
            context.set_state("voice_owner_seen", False)
            context.set_state("voice_owner_last_score", -1.0)

            def _append_semantic_frame(frame: Any) -> None:
                if not semantic_enabled:
                    return
                samples = [float(x) for x in np.asarray(frame, dtype=np.float32).reshape(-1)]
                if not samples:
                    return
                with semantic_lock:
                    semantic_buffer.extend(samples)
                    overflow = len(semantic_buffer) - semantic_max_samples
                    if overflow > 0:
                        del semantic_buffer[:overflow]

            def _snapshot_semantic_samples() -> list[float]:
                with semantic_lock:
                    if not semantic_buffer:
                        return []
                    return semantic_buffer[-semantic_window_samples:].copy()

            def _semantic_probe_worker() -> None:
                if not semantic_enabled:
                    return
                if context.args.debug_diagnostics:
                    context.on_state(
                        {
                            "event": "log",
                            "message": (
                                "diag semantic_gate enabled"
                                f" interval_s={semantic_probe_interval_s:.2f}"
                                f" window_s={semantic_window_s:.2f}"
                                f" end_silence_s={semantic_end_silence_s:.2f}"
                                f" min_chars={semantic_min_chars}"
                            ),
                        }
                    )
                while not context.stop_event.wait(semantic_probe_interval_s):
                    if not bool(context.get_state("voice_session_active")):
                        continue
                    if bool(context.get_state("voice_auto_stopping")):
                        continue
                    if owner_filter_enabled and not bool(context.get_state("voice_owner_active")):
                        continue
                    probe_samples = _snapshot_semantic_samples()
                    if len(probe_samples) < max(3200, int(sample_rate * 0.25)):
                        continue
                    text = _semantic_probe_text(
                        provider=context.provider,
                        samples=probe_samples,
                        sample_rate=sample_rate,
                        hotwords=context.resolve_hotwords(),
                        timeout_ms=semantic_timeout_ms,
                        normalize_final_text=context.normalize_final_text,
                    )
                    now_probe = time.monotonic()
                    if _semantic_text_has_content(text, min_chars=semantic_min_chars):
                        context.set_state("voice_semantic_has_text", True)
                        context.set_state("voice_semantic_last_text_ts", now_probe)
                        context.set_state("voice_semantic_last_text", text)
                        context.set_state("voice_speech_detected", True)
                        context.set_state("voice_last_speech_ts", now_probe)

            if semantic_enabled:
                threading.Thread(target=_semantic_probe_worker, daemon=True).start()

            def _emit_auto_stop(reason: str) -> bool:
                if bool(context.get_state("voice_auto_stopping")):
                    return False
                context.set_state("voice_auto_stopping", True)
                context.on_state({"event": "voice_wake_auto_stop", "reason": reason})
                threading.Thread(target=context.stop_recording, daemon=True).start()
                return True

            def _maybe_schedule_auto_stop(now_ts: float) -> bool:
                if not bool(context.get_state("voice_session_active")):
                    return False
                if bool(context.get_state("voice_auto_stopping")):
                    return False
                speech_detected = bool(context.get_state("voice_speech_detected"))
                started_ts = float(context.get_state("voice_started_ts"))
                if semantic_enabled:
                    no_speech_timeout_s = max(0.0, float(getattr(context.args, "wake_no_speech_timeout_s", 2.0)))
                    min_speech_s = max(0.0, float(getattr(context.args, "wake_min_speech_s", 0.5)))
                    acoustic_silence_s = max(0.0, float(getattr(context.args, "wake_auto_stop_silence_s", 1.5)))
                    semantic_has_text = bool(context.get_state("voice_semantic_has_text"))
                    semantic_last_ts = float(context.get_state("voice_semantic_last_text_ts"))
                    last_speech_ts = float(context.get_state("voice_last_speech_ts"))
                    semantic_reason = _should_auto_stop_semantic_session(
                        now_ts=now_ts,
                        started_ts=started_ts,
                        last_speech_ts=last_speech_ts,
                        semantic_has_text=semantic_has_text,
                        semantic_last_text_ts=semantic_last_ts,
                        no_speech_timeout_s=no_speech_timeout_s,
                        min_speech_s=min_speech_s,
                        semantic_end_silence_s=semantic_end_silence_s,
                        acoustic_silence_s=acoustic_silence_s,
                    )
                    if semantic_reason is not None:
                        return _emit_auto_stop(semantic_reason)
                    return False
                if not speech_detected:
                    no_speech_timeout_s = max(0.0, float(getattr(context.args, "wake_no_speech_timeout_s", 2.0)))
                    if no_speech_timeout_s > 0 and now_ts - started_ts >= no_speech_timeout_s:
                        return _emit_auto_stop("no_speech_timeout")
                    return False
                last_speech_ts = float(context.get_state("voice_last_speech_ts"))
                if now_ts - started_ts < max(0.0, float(getattr(context.args, "wake_min_speech_s", 0.5))):
                    return False

                base_silence_s = max(0.0, float(getattr(context.args, "wake_auto_stop_silence_s", 1.5)))
                owner_filter_enabled_runtime = bool(getattr(context.args, "wake_owner_verify", False))
                if owner_filter_enabled_runtime:
                    owner_active = bool(context.get_state("voice_owner_active"))
                    if owner_active:
                        owner_extend_s = max(0.0, float(getattr(context.args, "wake_owner_silence_extend_s", 0.5)))
                        silence_threshold = base_silence_s + owner_extend_s
                    else:
                        silence_threshold = base_silence_s * 0.6
                else:
                    silence_threshold = base_silence_s
                if now_ts - last_speech_ts < silence_threshold:
                    return False
                return _emit_auto_stop("silence")

            def _process_audio_frame(mono_frame: Any, *, frames: int, sample_rate: int) -> None:
                nonlocal noise_floor, smoothed_level, vad, vad_init_attempted, vad_log_emitted
                nonlocal speech_evidence_s, owner_audio_samples, owner_last_verify_ts, owner_last_active
                nonlocal owner_pass_streak, owner_fail_streak

                mono_frame = np.ascontiguousarray(np.asarray(mono_frame, dtype=np.float32).reshape(-1))
                if mono_frame.size == 0:
                    return
                rms = float(np.sqrt(np.mean(mono_frame ** 2)))
                _append_semantic_frame(mono_frame)

                if rms < noise_floor * 1.8:
                    noise_floor = noise_floor * 0.98 + rms * 0.02

                signal = max(0.0, rms - noise_floor * 1.1)
                linear = signal * 48.0
                level = linear / (linear + 0.15) if linear > 0.0 else 0.0

                owner_active = True
                if (
                    owner_filter_enabled
                    and owner_embeddings is not None
                    and _extract_owner_embedding is not None
                    and _owner_cosine_similarity is not None
                    and owner_audio_chunks is not None
                ):
                    owner_audio_chunks.append(mono_frame.copy())
                    owner_audio_samples += int(mono_frame.size)
                    while owner_audio_samples > owner_max_samples and owner_audio_chunks:
                        owner_audio_samples -= int(owner_audio_chunks.popleft().size)

                    now_owner = time.monotonic()
                    if now_owner - owner_last_verify_ts >= owner_verify_interval_s:
                        owner_last_verify_ts = now_owner
                        owner_score = -1.0
                        owner_active = bool(context.get_state("voice_owner_active"))
                        if owner_audio_samples >= owner_min_samples:
                            try:
                                verify_samples = np.concatenate(list(owner_audio_chunks))
                                if verify_samples.size > owner_max_samples:
                                    verify_samples = verify_samples[-owner_max_samples:]
                                candidate_embedding = _extract_owner_embedding(
                                    verify_samples,
                                    sample_rate=sample_rate,
                                    target_rate=sample_rate,
                                )
                                owner_score = max(
                                    float(_owner_cosine_similarity(candidate_embedding, owner_embedding))
                                    for owner_embedding in owner_embeddings
                                )
                                owner_upper = owner_threshold
                                owner_lower = max(0.0, owner_threshold - owner_deactivate_margin)
                                prev_owner_active = bool(context.get_state("voice_owner_active"))
                                if owner_score >= owner_upper:
                                    owner_pass_streak = min(owner_activate_confirm, owner_pass_streak + 1)
                                    owner_fail_streak = 0
                                elif owner_score < owner_lower:
                                    owner_fail_streak = min(owner_deactivate_confirm, owner_fail_streak + 1)
                                    owner_pass_streak = 0
                                else:
                                    owner_pass_streak = 0
                                    owner_fail_streak = 0

                                if prev_owner_active:
                                    owner_active = owner_fail_streak < owner_deactivate_confirm
                                else:
                                    owner_active = owner_pass_streak >= owner_activate_confirm
                            except Exception:
                                owner_active = False
                        context.set_state("voice_owner_active", owner_active)
                        context.set_state("voice_owner_last_score", owner_score)
                        if owner_active:
                            context.set_state("voice_owner_seen", True)
                        if context.args.debug_diagnostics and owner_active != owner_last_active:
                            context.on_state(
                                {
                                    "event": "log",
                                    "message": (
                                        "diag voice_owner_gate"
                                        f" active={owner_active}"
                                        f" score={owner_score:.3f}"
                                        f" threshold={owner_threshold:.3f}"
                                    ),
                                }
                            )
                        owner_last_active = owner_active
                    else:
                        owner_active = bool(context.get_state("voice_owner_active"))

                alpha = 0.28 if level > smoothed_level else 0.12
                smoothed_level = smoothed_level * (1.0 - alpha) + level * alpha
                display_level = _owner_gate_level(
                    smoothed_level,
                    owner_filter_enabled=owner_filter_enabled,
                    owner_active=owner_active,
                )
                context.on_state({"event": "audio_level", "level": display_level})

                if bool(context.get_state("voice_session_active")):
                    if use_webrtc_vad and vad is None and not vad_init_attempted:
                        vad_init_attempted = True
                        try:
                            import webrtcvad

                            vad = webrtcvad.Vad(vad_aggressiveness)
                            if context.args.debug_diagnostics and not vad_log_emitted:
                                context.on_state(
                                    {
                                        "event": "log",
                                        "message": (
                                            "diag voice_activity_detector mode=webrtcvad"
                                            f" sample_rate={vad_sample_rate}"
                                            f" frame_ms={vad_frame_ms}"
                                            f" aggressiveness={vad_aggressiveness}"
                                        ),
                                    }
                                )
                                vad_log_emitted = True
                        except Exception as exc:  # noqa: BLE001
                            vad = None
                            if context.args.debug_diagnostics and not vad_log_emitted:
                                context.on_state(
                                    {
                                        "event": "log",
                                        "message": (
                                            "diag voice_activity_detector_fallback=level "
                                            f"reason={type(exc).__name__}: {exc}"
                                        ),
                                    }
                                )
                                vad_log_emitted = True
                    elif context.args.debug_diagnostics and not use_webrtc_vad and not vad_log_emitted:
                        context.on_state({"event": "log", "message": "diag voice_activity_detector mode=level"})
                        vad_log_emitted = True

                    now_ts = time.monotonic()
                    speech_detected_raw = False
                    if vad is not None:
                        frame = mono_frame
                        if sample_rate != vad_sample_rate:
                            frame = _resample_audio_for_vad(frame, src_rate=sample_rate, dst_rate=vad_sample_rate)
                        vad_pcm_buffer.extend(_float_to_pcm16le(frame))
                        while len(vad_pcm_buffer) >= vad_frame_bytes:
                            frame_bytes = bytes(vad_pcm_buffer[:vad_frame_bytes])
                            del vad_pcm_buffer[:vad_frame_bytes]
                            if vad.is_speech(frame_bytes, vad_sample_rate):
                                speech_detected_raw = True
                    elif _is_level_speech_frame(level=level, rms=rms, noise_floor=noise_floor):
                        speech_detected_raw = True
                    if owner_filter_enabled and not owner_active:
                        speech_detected_raw = False

                    block_duration_s = max(0.0, float(frames) / float(sample_rate)) if sample_rate > 0 else 0.0
                    speech_evidence_s, speech_detected = _update_speech_evidence(
                        speech_evidence_s,
                        speech_detected_raw=speech_detected_raw,
                        frame_duration_s=block_duration_s,
                        confirm_s=wake_speech_confirm_s,
                    )

                    if speech_detected:
                        context.set_state("voice_last_speech_ts", now_ts)
                        if not semantic_enabled:
                            context.set_state("voice_speech_detected", True)

            if monitor_stream is not None:
                frame_bytes = max(1, monitor_channels) * 4
                read_frames = 1024
                pending = bytearray()
                while not context.stop_event.is_set():
                    chunk = monitor_stream.read(read_frames * frame_bytes)
                    if chunk:
                        pending.extend(chunk)
                    elif context.record_handle.process.poll() is not None:
                        break
                    else:
                        if _maybe_schedule_auto_stop(time.monotonic()):
                            break
                        context.stop_event.wait(0.02)
                        continue

                    full_bytes = (len(pending) // frame_bytes) * frame_bytes
                    if full_bytes <= 0:
                        continue
                    raw = bytes(pending[:full_bytes])
                    del pending[:full_bytes]
                    samples = np.frombuffer(raw, dtype=np.float32)
                    if monitor_channels > 1:
                        samples = samples.reshape(-1, monitor_channels).mean(axis=1)
                    _process_audio_frame(samples, frames=int(samples.size), sample_rate=sample_rate)
                    if _maybe_schedule_auto_stop(time.monotonic()):
                        break
            else:
                import sounddevice as sd

                def _cb(indata: Any, frames: int, t: Any, status: Any) -> None:
                    if context.stop_event.is_set():
                        raise sd.CallbackStop()
                    _process_audio_frame(indata, frames=frames, sample_rate=sample_rate)

                with sd.InputStream(
                    device=device_id,
                    samplerate=sample_rate,
                    channels=1,
                    blocksize=1024,
                    callback=_cb,
                ):
                    while not context.stop_event.wait(0.05):
                        if _maybe_schedule_auto_stop(time.monotonic()):
                            break
        except ImportError:
            if context.args.debug_diagnostics:
                context.on_state({"event": "log", "message": "diag audio_level_monitoring_disabled: sounddevice not installed"})
        except Exception as exc:  # noqa: BLE001
            if context.args.debug_diagnostics:
                context.on_state({"event": "log", "message": f"diag audio_level_monitoring_failed: {exc}"})

    thread = threading.Thread(target=_level_worker, name="recordian-wake-monitor", daemon=True)
    thread.start()
    return thread
