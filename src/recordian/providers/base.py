from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..models import ASRResult


@dataclass(frozen=True, slots=True)
class ASRProviderCapabilities:
    supports_hotwords: bool = True
    supports_context: bool = False
    supports_language_hint: bool = False
    supports_file_streaming: bool = False
    supports_realtime: bool = False


class ASRProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @property
    def capabilities(self) -> ASRProviderCapabilities:
        return ASRProviderCapabilities()

    @property
    def is_cloud(self) -> bool:
        return False

    def supports_file_streaming(self) -> bool:
        return self.capabilities.supports_file_streaming

    def supports_realtime_transcription(self) -> bool:
        return self.capabilities.supports_realtime

    @abstractmethod
    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        raise NotImplementedError


def provider_supports_realtime(provider: object) -> bool:
    method = getattr(provider, "supports_realtime_transcription", None)
    if callable(method):
        try:
            return bool(method())
        except Exception:
            return False
    capabilities = getattr(provider, "capabilities", None)
    return bool(getattr(capabilities, "supports_realtime", False))


def provider_supports_file_streaming(provider: object) -> bool:
    method = getattr(provider, "supports_file_streaming", None)
    if callable(method):
        try:
            return bool(method())
        except Exception:
            return False
    capabilities = getattr(provider, "capabilities", None)
    if getattr(capabilities, "supports_file_streaming", False):
        return True
    return hasattr(provider, "transcribe_file_stream")


def _estimate_english_ratio(text: str) -> float:
    if not text:
        return 0.0
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    alpha = sum(1 for ch in text if ch.isalpha())
    if alpha == 0:
        return 0.0
    return latin / alpha
