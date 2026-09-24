"""Protective recording duration limit for the Confucius streaming ASR path.

The local Confucius streaming server enforces a hard per-session audio
budget (``server/confucius_streaming_server.py``
``DEFAULT_MAX_SESSION_SECONDS = 30.0``). A session that crosses it is
rejected with ``session audio budget exceeded`` and the audio already
recognized in that session is lost: the client suppresses the fallback
(``path=realtime_stale_suppressed``) and the whole turn comes back empty.

This module is the SINGLE client-side source of the protective limit:
recording is ended through the existing normal stop/EOS path comfortably
before the server budget is hit. It is an explicitly documented protective
segment limit — not seamless segmentation, and it never restarts the
microphone by itself.

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

_CONFUCIUS_PROVIDER_NAME = "confucius-asr"


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
