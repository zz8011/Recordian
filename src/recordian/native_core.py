"""Versioned Rust audio ABI. Build before use; no compilation in the audio path."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
MAX_AUDIO_BYTES = 64 * 1024 * 1024
ABI_VERSION = 1
_scratch = threading.local()


@lru_cache(maxsize=8)
def _load(mode: str, filename: str) -> ctypes.CDLL | None:
    if mode not in {"auto", "required", "python"}:
        raise ValueError("RECORDIAN_NATIVE_CORE must be auto, required or python")
    if mode == "python":
        return None
    try:
        library = ctypes.CDLL(filename)
        library.recordian_core_abi_version.argtypes = []
        library.recordian_core_abi_version.restype = ctypes.c_uint32
        if library.recordian_core_abi_version() != ABI_VERSION:
            raise RuntimeError("unsupported Recordian native ABI")
        signatures = {
            "recordian_audio_convert": (
                [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)],
                ctypes.c_int,
            ),
            "recordian_audio_rms": ([ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
            "recordian_pcm_buffer_new": ([ctypes.c_size_t], ctypes.c_void_p),
            "recordian_pcm_buffer_push": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t], ctypes.c_int),
            "recordian_pcm_buffer_pop": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int),
            "recordian_pcm_buffer_len": ([ctypes.c_void_p], ctypes.c_size_t),
            "recordian_pcm_buffer_clear": ([ctypes.c_void_p], None),
            "recordian_pcm_buffer_free": ([ctypes.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(library, name)
            fn.argtypes = args
            fn.restype = result
    except (OSError, AttributeError, RuntimeError) as exc:
        if mode == "required":
            raise RuntimeError(f"Recordian Rust core unavailable: {exc}") from exc
        logger.debug("Native core unavailable; using Python: %s", exc)
        return None
    logger.info("Recordian Rust core loaded (ABI %s): %s", ABI_VERSION, filename)
    return library


def load_library() -> ctypes.CDLL | None:
    filename = os.environ.get("RECORDIAN_NATIVE_LIBRARY") or str(
        Path(__file__).with_name("_native") / "librecordian_core.so"
    )
    return _load(os.environ.get("RECORDIAN_NATIVE_CORE", "auto").strip().lower(), filename)


def status() -> dict[str, Any]:
    library = load_library()
    return {
        "backend": "rust" if library else "python",
        "abi": ABI_VERSION if library else None,
        "library": library._name if library else None,
    }


def convert_audio(raw: bytes) -> tuple[bytes, float] | None:
    library = load_library()
    if library is None:
        return None
    if len(raw) % 4 or len(raw) > MAX_AUDIO_BYTES:
        raise ValueError(f"f32le audio must be a multiple of 4 bytes and within 64 MiB, got {len(raw)}")
    size = len(raw) // 2
    buffer = getattr(_scratch, "pcm", None)
    if buffer is None or len(buffer) < size:
        buffer = ctypes.create_string_buffer(max(1, size))
        # Keep at most 1 MiB per worker; larger files use temporary storage.
        if size <= 1024 * 1024:
            _scratch.pcm = buffer
    level = ctypes.c_double()
    if library.recordian_audio_convert(raw, len(raw), buffer, len(buffer), ctypes.byref(level)):
        raise ValueError("native audio conversion rejected input")
    return ctypes.string_at(buffer, size), level.value


def audio_rms(raw: bytes) -> float | None:
    library = load_library()
    if library is None:
        return None
    level = ctypes.c_double()
    if library.recordian_audio_rms(raw, len(raw), ctypes.byref(level)):
        raise ValueError("native RMS rejected input")
    return level.value


class PcmBuffer:
    """Fixed-capacity PCM FIFO. Every returned array has independent ownership."""

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes <= 0 or capacity_bytes % 2 or capacity_bytes > MAX_AUDIO_BYTES:
            raise ValueError("invalid PCM capacity")
        self._lock = threading.RLock()
        self._closed = False
        self._capacity = capacity_bytes
        self._library = None
        self._handle = None
        self._python = bytearray()
        self._library = load_library()
        if self._library is not None:
            self._handle = self._library.recordian_pcm_buffer_new(capacity_bytes)
            if not self._handle:
                raise MemoryError("native PCM buffer allocation failed")

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("PCM buffer is closed")

    def __len__(self) -> int:
        with self._lock:
            self._check_open()
            if self._library is not None:
                return int(self._library.recordian_pcm_buffer_len(self._handle))
            return len(self._python)

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._check_open()
            if len(pcm) % 2 or len(pcm) > self._capacity - len(self):
                raise ValueError("partial PCM sample or bounded buffer full")
            if self._library is not None:
                if self._library.recordian_pcm_buffer_push(self._handle, pcm, len(pcm)):
                    raise ValueError("native PCM buffer rejected input")
            else:
                self._python.extend(pcm)

    def pop_float(self, samples: int) -> Any:
        import numpy as np

        with self._lock:
            self._check_open()
            if samples < 0 or samples > len(self) // 2:
                raise ValueError("insufficient PCM samples")
            if self._library is not None:
                result = np.empty(samples, dtype=np.float32)
                if self._library.recordian_pcm_buffer_pop(self._handle, result.ctypes.data, samples):
                    raise ValueError("native PCM buffer rejected read")
                return result
            size = samples * 2
            result = np.frombuffer(bytes(self._python[:size]), dtype="<i2").astype(np.float32) / 32768.0
            del self._python[:size]
            return result

    def clear(self) -> None:
        with self._lock:
            self._check_open()
            if self._library is not None:
                self._library.recordian_pcm_buffer_clear(self._handle)
            else:
                self._python.clear()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._library is not None and self._handle:
                self._library.recordian_pcm_buffer_free(self._handle)
                self._handle = None
            self._python.clear()
            self._closed = True

    def __enter__(self) -> PcmBuffer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "_lock"):
            self.close()


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False))
