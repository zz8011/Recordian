"""Thread-safe state management for Recordian hotkey dictation.

This module centralises all mutable state used by the dictation engine into a
single, type-annotated :class:`DictationState` dataclass guarded by a
reentrant lock.  A module-level singleton (:data:`state_manager`) is provided
so that existing call-sites can migrate from the ad-hoc ``_get_state`` /
``_set_state`` helpers with minimal diff.

Public API
----------
:class:`RecordingState`      – IDLE / RECORDING / PROCESSING enum
:class:`RealtimeASRWorkerHandle` – thread handle + result metadata
:class:`DictationState`      – frozen-snapshot dataclass of *all* state
:class:`StateManager`        – thread-safe wrapper (get / set / update / reset)
:data:`state_manager`        – module-level singleton instance
"""

from __future__ import annotations

import enum
import subprocess
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# ---------------------------------------------------------------------------
# Enums & small data classes
# ---------------------------------------------------------------------------

class RecordingState(enum.Enum):
    """Recording lifecycle stages."""

    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


@dataclass(slots=True)
class RealtimeASRWorkerHandle:
    """Handle returned when a realtime ASR worker thread is spawned."""

    thread: threading.Thread
    final_text: str = ""
    detected_language: str = ""
    transcribe_latency_ms: float = 0.0
    commit_info: dict[str, object] | None = None
    error: str = ""


# ---------------------------------------------------------------------------
# DictationState – the single source of truth
# ---------------------------------------------------------------------------

@dataclass
class DictationState:
    """All mutable state for the hotkey-dictation engine.

    Every field carries a sensible default so a freshly-constructed instance
    represents the "idle / reset" state.
    """

    # -- timing & trigger ---------------------------------------------------
    last_trigger: float = 0.0
    """monotonic timestamp of the most recent hotkey / voice-wake trigger."""

    # -- recording subprocess -----------------------------------------------
    process: subprocess.Popen | None = None
    """The active audio-recording sub-process (ffmpeg / parecord …)."""

    temp_dir: TemporaryDirectory | None = None
    """Temporary directory backing *audio_path* (cleaned up on reset)."""

    audio_path: Path | None = None
    """Filesystem path to the in-progress recording."""

    record_started_at: float | None = None
    """``time.perf_counter()`` at recording start."""

    target_window_id: str | int | None = None
    """X11 / Wayland window ID that had focus when recording began."""

    # -- audio-level sampling -----------------------------------------------
    level_stop: threading.Event | None = None
    """Event used to signal the audio-level sampling thread to stop."""

    # -- post-processing ----------------------------------------------------
    processing_thread: threading.Thread | None = None
    """Background thread performing ASR + text-commit."""

    # -- state machine ------------------------------------------------------
    recording_state: RecordingState = RecordingState.IDLE
    record_source: str = "hotkey"
    """``"hotkey"`` or ``"voice_wake"`` – how the current session started."""

    # -- voice-wake detection -----------------------------------------------
    voice_session_active: bool = False
    voice_last_speech_ts: float = 0.0
    voice_started_ts: float = 0.0
    voice_speech_detected: bool = False
    voice_auto_stopping: bool = False

    # -- semantic gating (voice wake) ---------------------------------------
    voice_semantic_enabled: bool = False
    voice_semantic_has_text: bool = False
    voice_semantic_last_text_ts: float = 0.0
    voice_semantic_last_text: str = ""

    # -- owner-filter (voice wake) ------------------------------------------
    voice_owner_filter_enabled: bool = False
    voice_owner_active: bool = True
    voice_owner_seen: bool = False
    voice_owner_last_score: float = -1.0

    # -- realtime ASR -------------------------------------------------------
    realtime_asr_worker: RealtimeASRWorkerHandle | None = None
    """Handle for the streaming / realtime ASR worker thread."""


# ---------------------------------------------------------------------------
# StateManager – thread-safe wrapper
# ---------------------------------------------------------------------------

class StateManager:
    """Thread-safe manager around a :class:`DictationState` instance.

    All reads and writes acquire an internal :class:`threading.RLock` so that
    nested calls (e.g. from within ``transition_to_idle``) never deadlock.

    Example::

        from recordian.state_manager import state_manager

        state_manager.set("recording_state", RecordingState.RECORDING)
        ...
        state_manager.transition_to_idle()
    """

    __slots__ = ("_state", "_lock")

    def __init__(self) -> None:
        self._state: DictationState = DictationState()
        self._lock: threading.RLock = threading.RLock()

    # -- single-key access --------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Read a state field by name (thread-safe).

        Returns *default* when the key does not map to a dataclass field.
        """
        with self._lock:
            if hasattr(self._state, key):
                return getattr(self._state, key)
            return default

    def set(self, key: str, value: Any) -> None:
        """Write a state field by name (thread-safe).

        Raises :class:`AttributeError` if *key* is not a valid field.
        """
        with self._lock:
            if not hasattr(self._state, key):
                raise AttributeError(
                    f"DictationState has no field {key!r}. "
                    f"Valid fields: {[f.name for f in fields(self._state)]}"
                )
            setattr(self._state, key, value)

    # -- bulk update --------------------------------------------------------

    def update(self, updates: dict[str, Any]) -> None:
        """Apply a batch of ``(key, value)`` pairs atomically."""
        with self._lock:
            for key, value in updates.items():
                setattr(self._state, key, value)

    # -- lifecycle helpers --------------------------------------------------

    def transition_to_idle(self) -> None:
        """Reset all per-session state to idle defaults.

        This is the moral equivalent of the old ``_transition_to_idle()``
        helper in ``hotkey_dictate.py``.
        """
        with self._lock:
            self._state.process = None
            self._state.temp_dir = None
            self._state.audio_path = None
            self._state.record_started_at = None
            self._state.level_stop = None
            self._state.recording_state = RecordingState.IDLE
            self._state.record_source = "hotkey"
            self._state.voice_session_active = False
            self._state.voice_last_speech_ts = 0.0
            self._state.voice_started_ts = 0.0
            self._state.voice_speech_detected = False
            self._state.voice_auto_stopping = False
            self._state.voice_semantic_enabled = False
            self._state.voice_semantic_has_text = False
            self._state.voice_semantic_last_text_ts = 0.0
            self._state.voice_semantic_last_text = ""
            self._state.voice_owner_filter_enabled = False
            self._state.voice_owner_active = True
            self._state.voice_owner_seen = False
            self._state.voice_owner_last_score = -1.0
            self._state.realtime_asr_worker = None

    # -- snapshot -----------------------------------------------------------

    def snapshot(self) -> DictationState:
        """Return a shallow copy of the current state (thread-safe read).

        Because :class:`DictationState` is a regular dataclass you can pass
        the returned object around without worrying about lock contention.
        """
        import dataclasses as _dc

        with self._lock:
            return _dc.replace(self._state)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

state_manager: StateManager = StateManager()


# ---------------------------------------------------------------------------
# Backward-compatible free functions
# ---------------------------------------------------------------------------
# These thin wrappers let callers that previously did
# ``from recordian.hotkey_dictate import _get_state, _set_state, …`` migrate
# to ``from recordian.state_manager import get_state, set_state, …`` with a
# trivial find-replace (drop the leading underscore).

def get_state(key: str, default: Any = None) -> Any:
    """Thread-safe state read – delegates to the global :data:`state_manager`."""
    return state_manager.get(key, default)


def set_state(key: str, value: Any) -> None:
    """Thread-safe state write – delegates to the global :data:`state_manager`."""
    state_manager.set(key, value)


def update_state(updates: dict[str, Any]) -> None:
    """Thread-safe bulk state update – delegates to the global :data:`state_manager`."""
    state_manager.update(updates)


def transition_to_idle() -> None:
    """Reset per-session state – delegates to the global :data:`state_manager`."""
    state_manager.transition_to_idle()
