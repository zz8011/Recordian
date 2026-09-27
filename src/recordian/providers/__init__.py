"""ASR provider / text refiner registry.

Heavy provider modules are imported lazily (PEP 562 ``__getattr__``) so a
basic install — without optional extras such as ``websocket-client`` or
``numpy`` — can always ``import recordian.providers`` and use the light
base classes. The ImportError for a missing extra surfaces only when the
specific provider is actually constructed.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from .base import (
    ASRProvider,
    ASRProviderCapabilities,
    provider_supports_file_streaming,
    provider_supports_realtime,
)
from .base_text_refiner import BaseTextRefiner
from .streaming_base import StreamingASRProvider

if TYPE_CHECKING:
    from .cloud_llm_refiner import CloudLLMRefiner
    from .confucius_asr import ConfuciusASRProvider, ConfuciusProtocolError
    from .http_cloud import HttpCloudProvider
    from .llamacpp_text_refiner import LlamaCppTextRefiner
    from .qwen_asr import QwenASRProvider
    from .qwen_text_refiner import Qwen3TextRefiner

_LAZY_EXPORTS = {
    "CloudLLMRefiner": "cloud_llm_refiner",
    "ConfuciusASRProvider": "confucius_asr",
    "ConfuciusProtocolError": "confucius_asr",
    "HttpCloudProvider": "http_cloud",
    "LlamaCppTextRefiner": "llamacpp_text_refiner",
    "QwenASRProvider": "qwen_asr",
    "Qwen3TextRefiner": "qwen_text_refiner",
}

__all__ = [
    "ASRProvider",
    "ASRProviderCapabilities",
    "provider_supports_file_streaming",
    "provider_supports_realtime",
    "BaseTextRefiner",
    "StreamingASRProvider",
    "HttpCloudProvider",
    "ConfuciusASRProvider",
    "ConfuciusProtocolError",
    "QwenASRProvider",
    "Qwen3TextRefiner",
    "CloudLLMRefiner",
    "LlamaCppTextRefiner",
]


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache: only one import per name
    return value
