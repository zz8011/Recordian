"""Recording state enum for Recordian dictation."""
from __future__ import annotations

import enum


class RecordingState(enum.Enum):
    """Recording lifecycle stages."""

    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
