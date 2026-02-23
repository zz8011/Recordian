from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import ASRResult


class ASRProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @property
    def is_cloud(self) -> bool:
        return False

    @abstractmethod
    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        raise NotImplementedError


def _estimate_english_ratio(text: str) -> float:
    if not text:
        return 0.0
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    alpha = sum(1 for ch in text if ch.isalpha())
    if alpha == 0:
        return 0.0
    return latin / alpha
