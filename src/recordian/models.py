from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

try:
    from enum import StrEnum  # type: ignore[attr-defined]
except ImportError:
    class StrEnum(str, Enum):
        """Python 3.10 fallback for enum.StrEnum."""


class SessionState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    STREAMING = "streaming"
    END_DETECTED = "end_detected"
    CORRECTING = "correcting"
    COMMIT = "commit"


@dataclass(slots=True)
class ASRResult:
    text: str
    confidence: float | None = None
    english_ratio: float = 0.0
    model_name: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class SessionContext:
    hotwords: list[str] = field(default_factory=list)
    force_high_precision: bool = False


@dataclass(slots=True)
class Decision:
    run_pass2: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CommitResult:
    state: SessionState
    text: str
    pass1_result: ASRResult
    pass2_result: ASRResult | None
    decision: Decision


@dataclass(slots=True)
class StreamUpdate:
    text: str
    is_final: bool
    chunk_index: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RealtimeRunResult:
    updates: list[StreamUpdate]
    commit: CommitResult
