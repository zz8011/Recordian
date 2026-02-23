from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ASRResult, StreamUpdate


class StreamingASRProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def start_session(self, *, hotwords: list[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def push_chunk(self, samples: list[float], *, is_final: bool, chunk_index: int) -> StreamUpdate | None:
        raise NotImplementedError

    @abstractmethod
    def end_session(self) -> ASRResult:
        raise NotImplementedError
