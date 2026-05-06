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
class ASRSegment:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def _coerce_optional_ms(value: object) -> int | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    return int(round(numeric))


def coerce_asr_timestamps(value: object) -> list[tuple[int | None, int | None]]:
    if not isinstance(value, list):
        return []

    timestamps: list[tuple[int | None, int | None]] = []
    for item in value:
        start_ms: int | None = None
        end_ms: int | None = None
        if isinstance(item, dict):
            start_ms = _coerce_optional_ms(item.get("start_ms", item.get("start")))
            end_ms = _coerce_optional_ms(item.get("end_ms", item.get("end")))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start_ms = _coerce_optional_ms(item[0])
            end_ms = _coerce_optional_ms(item[1])
        if start_ms is None and end_ms is None:
            continue
        timestamps.append((start_ms, end_ms))
    return timestamps


def coerce_asr_segments(value: object) -> list[ASRSegment]:
    if not isinstance(value, list):
        return []

    segments: list[ASRSegment] = []
    for item in value:
        if isinstance(item, ASRSegment):
            segments.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        metadata = {
            key: raw
            for key, raw in item.items()
            if key not in {"text", "start", "start_ms", "end", "end_ms", "speaker"}
        }
        segments.append(
            ASRSegment(
                text=text,
                start_ms=_coerce_optional_ms(item.get("start_ms", item.get("start"))),
                end_ms=_coerce_optional_ms(item.get("end_ms", item.get("end"))),
                speaker=str(item.get("speaker")).strip() or None if item.get("speaker") is not None else None,
                metadata=metadata,
            )
        )
    return segments


@dataclass(slots=True)
class ASRResult:
    text: str
    confidence: float | None = None
    english_ratio: float = 0.0
    model_name: str = ""
    raw_text: str = ""
    normalized_text: str = ""
    detected_language: str | None = None
    timestamps: list[tuple[int | None, int | None]] = field(default_factory=list)
    segments: list[ASRSegment] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.raw_text:
            self.raw_text = self.text
        if not self.normalized_text:
            self.normalized_text = self.text
        if self.segments:
            self.segments = coerce_asr_segments(self.segments)
        if self.timestamps:
            self.timestamps = coerce_asr_timestamps(self.timestamps)
        elif self.segments:
            self.timestamps = [(segment.start_ms, segment.end_ms) for segment in self.segments]


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
