"""Protective recording duration limit for the Confucius streaming ASR path.

The local Confucius streaming server enforces a hard per-session audio
budget (``server/confucius_streaming_server.py``
``DEFAULT_MAX_SESSION_SECONDS = 30.0``). A session that crosses it is
rejected with ``session audio budget exceeded`` and the audio already
recognized in that session is lost: the client suppresses the fallback
(``path=realtime_stale_suppressed``) and the whole turn comes back empty.

This module is the SINGLE client-side source of the protective numbers.
``CONFUCIUS_RECORDING_LIMIT_S`` (25s) is only for a turn whose realtime
worker was NOT created (oneshot / non-streaming fallback). An active
Confucius realtime worker keeps the one microphone and rotates ASR
sessions on sample counts instead: soft cut at
``CONTINUOUS_SEGMENT_MIN_S`` on a silence run, hard cut at
``CONTINUOUS_SEGMENT_MAX_S`` (still under the 30s server budget).
The guard never restarts the microphone by itself.

The server module cannot be imported here (it pulls in the GPU inference
stack), so the budget value is mirrored below with a two-way anchor
comment. If the server default ever changes, change BOTH places.

No settings are added on purpose: the limit is fixed so it cannot drift
away from the server budget via user configuration.
"""
from __future__ import annotations

# Mirrors server/confucius_streaming_server.py DEFAULT_MAX_SESSION_SECONDS.
CONFUCIUS_SERVER_SESSION_BUDGET_S = 30.0

# Budget minus a conservative margin. The server budget counts AUDIO SAMPLES
# fed to the session (max_samples at 16 kHz), not model wall-clock time — the
# final inference after EOS is not bounded by it. The margin therefore covers
# capture/buffering only: audio already captured by the recorder and sitting
# in the monitor pipe but not yet fed, 160 ms chunk scheduling, and recorder
# teardown latency, so that fed samples stay below the budget.
CONFUCIUS_RECORDING_LIMIT_S = 25.0

# Continuous worker rotation (audio samples, not a second mic and not a
# raised server budget). Silence window is 400 ms, inside the 320–480 ms
# band. Hard cap stays 6 s under the 30 s server sample budget; the soft
# cut only rotates EARLIER than before (8 s instead of 15 s on a silence
# run), so the per-session sample budget is respected with more margin,
# not less. Shorter segments also keep each ASR session in the range
# where the small streaming model is most accurate.
CONTINUOUS_SEGMENT_MIN_S = 8.0
CONTINUOUS_SEGMENT_MAX_S = 24.0
CONTINUOUS_SILENCE_S = 0.40
# Silence detection is noise-adaptive (see AdaptiveSilenceMeter): the
# threshold follows the turn's own noise floor instead of one absolute
# RMS value, which a quiet speaker in a quiet room could never cross
# downward and steady room noise could never cross upward. The factor
# and floor mirror the wake monitor's adaptive precedent
# (wake_session_monitor: rms >= max(0.0025, floor * 2.2)).
CONTINUOUS_SILENCE_FACTOR = 2.2
CONTINUOUS_SILENCE_ABS_MIN = 0.0025
CONTINUOUS_SILENCE_ABS_MAX = 0.03
CONTINUOUS_SILENCE_FLOOR_EMA = 0.15
CONTINUOUS_HELD_TAIL_CHARS = 64
CONTINUOUS_CONTEXT_CHARS = 256
CONTINUOUS_PREEDIT_CHARS = 512
# The native composition token dies after 120 s of inactivity. During long
# silence no segment commits happen, so the worker re-touches the SAME token
# with an empty/current preedit refresh on this audio-counted interval
# (no text written, no new BeginSession).
CONTINUOUS_IME_REFRESH_S = 30.0
# Per-reader fanout backlog. The pump never blocks; the reader errors.
MONITOR_BACKLOG_S = 8.0

_CONFUCIUS_PROVIDER_NAME = "confucius-asr"


class AdaptiveSilenceMeter:
    """Noise-adaptive silence classification for continuous segmentation.

    Tracks an EMA noise floor from the pieces classified as silence and
    classifies a piece as silence when its RMS falls below
    ``clamp(floor * FACTOR, ABS_MIN, ABS_MAX)``. The floor only learns
    from silence-classified pieces, so speech can never inflate it; a
    turn that starts in steady noise converges to that noise level and
    can still cut on pauses, while a quiet speaker in a quiet room gets
    the ABS_MIN threshold instead of an unreachable absolute value.
    One meter lives for one dictation turn (the room does not change
    mid-turn); it is deliberately not shared across turns.
    """

    def __init__(
        self,
        *,
        factor: float = CONTINUOUS_SILENCE_FACTOR,
        abs_min: float = CONTINUOUS_SILENCE_ABS_MIN,
        abs_max: float = CONTINUOUS_SILENCE_ABS_MAX,
        ema_alpha: float = CONTINUOUS_SILENCE_FLOOR_EMA,
    ) -> None:
        self._factor = max(1.0, float(factor))
        self._abs_min = max(0.0, float(abs_min))
        self._abs_max = max(self._abs_min, float(abs_max))
        self._ema_alpha = min(1.0, max(0.0, float(ema_alpha)))
        self._floor: float | None = None

    @property
    def noise_floor(self) -> float | None:
        return self._floor

    @property
    def threshold(self) -> float:
        if self._floor is None:
            return self._abs_min
        return min(self._abs_max, max(self._abs_min, self._floor * self._factor))

    def observe(self, rms: float) -> bool:
        """Feed one piece's RMS; return True when the piece is silence."""
        value = max(0.0, float(rms))
        if self._floor is None:
            # First piece seeds the floor; it is never classified silence
            # on its own evidence (a turn starting mid-speech must not be
            # mistaken for a pause).
            self._floor = value
            return False
        is_silence = value < self.threshold
        if is_silence:
            alpha = self._ema_alpha
            self._floor = (1.0 - alpha) * self._floor + alpha * value
        return is_silence


def recording_limit_s_for_provider(provider: object) -> float | None:
    """Protective per-recording limit for providers behind a session budget.

    Returns ``None`` for every other provider: their behaviour is unchanged
    and no timer is armed. Applies regardless of ``enable_streaming_commit``
    because the Confucius file-transcription fallback speaks the same
    budgeted protocol.
    """
    if getattr(provider, "provider_name", "") == _CONFUCIUS_PROVIDER_NAME:
        return CONFUCIUS_RECORDING_LIMIT_S
    return None


def clamp_oneshot_duration_s(asr_provider: str, duration_s: float | None) -> float | None:
    """Clamp a oneshot (--duration) recording to the Confucius limit.

    Oneshot mode records a fixed length and then transcribes the whole file
    through the same 30 s session protocol, so a configured duration beyond
    the budget would lose the entire take. Returns the duration unchanged
    for other providers or durations already within the limit.
    """
    if asr_provider != _CONFUCIUS_PROVIDER_NAME:
        return duration_s
    if duration_s is None or duration_s > CONFUCIUS_RECORDING_LIMIT_S:
        return CONFUCIUS_RECORDING_LIMIT_S
    return duration_s
