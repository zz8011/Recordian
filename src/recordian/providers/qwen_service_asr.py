"""Client provider for the Qwen3-ASR production service (server/qwen_asr_service.py).

The service hosts the eval harness's B-arm ``QwenASRProvider`` unchanged;
this class is the pipeline-side half: a plain HTTP client (main venv, no
torch) implementing the repo's ASRProvider contract — ``transcribe_file``
plus the realtime session shape the dictation workers consume
(``start_realtime_session`` / ``push_audio`` / ``finish`` / ``cancel``).

Qwen3-ASR decodes whole segments, it does not stream tokens. The session
therefore buffers the press and decodes it whole in ``finish()``. Partial
previews are DEGRADED, on purpose and documented in the design
(~/.local/state/recordian/qwen-asr-prod/DESIGN.md): a background thread
re-decodes the accumulated audio at most every ``preview_interval_s``
(and only up to ``preview_max_audio_s`` of audio), exactly the preview
model the in-process ``QwenASRProvider`` session already uses.
``push_audio`` never blocks on a decode.
"""
from __future__ import annotations

import array
import base64
import io
import logging
import math
import struct
import threading
import time
import wave
from pathlib import Path

from ..local_auth import load_private_token
from ..models import ASRResult
from .base import ASRProvider, ASRProviderCapabilities, _estimate_english_ratio

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DEFAULT_ENDPOINT = "http://127.0.0.1:8323"
DEFAULT_TOKEN_FILE = "~/.config/recordian/qwen_service_token.txt"

# finish() decodes in requests of at most this much audio (the service
# rejects > 300 s): beyond it the press is split and the chunk texts are
# concatenated, the same seam behaviour the A/B harness uses for its
# Confucius arms. 150 s stays near the longest eval-validated clip (98 s)
# while keeping one decode's worst-case wait bounded.
_FINISH_CHUNK_S = 150.0
_BUSY_RETRY_S = 0.25


def f32le_to_pcm16le(raw: bytes) -> bytes:
    """f32le/16k/mono worker audio -> PCM16 LE (stdlib only, clipped)."""
    if not raw:
        return b""
    if len(raw) % 4 != 0:
        raise ValueError(f"f32le audio must be a multiple of 4 bytes, got {len(raw)}")
    samples = struct.unpack(f"<{len(raw) // 4}f", raw)
    pcm = array.array("h")
    append = pcm.append
    for value in samples:
        if not math.isfinite(value):
            append(0)
        elif value > 1.0:
            append(32767)
        elif value < -1.0:
            append(-32768)
        else:
            append(int(value * 32767))
    if struct.pack("=h", 1) != struct.pack("<h", 1):
        pcm.byteswap()
    return pcm.tobytes()


def pcm16le_to_wav_bytes(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class QwenServiceASRProvider(ASRProvider):
    """Qwen3-ASR via the local production service (see module docstring)."""

    # The realtime worker reads this for its monitor chunk sizing.
    realtime_chunk_size_sec = 0.5

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        token_file: str = DEFAULT_TOKEN_FILE,
        timeout_s: float = 60.0,
        context: str = "",
        preview_interval_s: float = 2.5,
        preview_min_new_s: float = 1.5,
        preview_max_audio_s: float = 30.0,
        preview_timeout_s: float = 20.0,
        http_session: object | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token_file = token_file
        self.timeout_s = float(timeout_s)
        self.context = context
        self.preview_interval_s = float(preview_interval_s)
        self.preview_min_new_s = float(preview_min_new_s)
        self.preview_max_audio_s = float(preview_max_audio_s)
        self.preview_timeout_s = float(preview_timeout_s)
        self._http_session = http_session
        self._token: str | None = None

    @property
    def provider_name(self) -> str:
        return "qwen-asr-service"

    @property
    def capabilities(self) -> ASRProviderCapabilities:
        return ASRProviderCapabilities(
            supports_hotwords=True,
            supports_context=True,
            supports_language_hint=True,
            supports_realtime=True,
        )

    # -- transport ---------------------------------------------------------

    def _session(self):  # noqa: ANN202 - requests.Session, lazily imported like http_cloud
        if self._http_session is None:
            import requests

            self._http_session = requests.Session()
        return self._http_session

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = ""
            if self.token_file:
                path = Path(self.token_file).expanduser()
                if path.exists():
                    self._token = load_private_token(str(path))
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _post_transcribe(self, wav_bytes: bytes, hotwords: list[str], *, timeout_s: float) -> dict:
        """POST one decode; 429 (service busy) retries until the deadline."""
        payload = {
            "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
            "hotwords": list(hotwords),
            "context": self.context,
        }
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"qwen-asr-service did not accept the request within {timeout_s:.0f}s")
            try:
                response = self._session().post(
                    f"{self.endpoint}/transcribe",
                    json=payload,
                    headers=self._auth_headers(),
                    timeout=max(1.0, remaining),
                )
            except Exception as exc:  # noqa: BLE001 - normalised for the pipeline's error surface
                raise RuntimeError(f"qwen-asr-service unreachable: {type(exc).__name__}") from exc
            if response.status_code == 429:
                time.sleep(min(_BUSY_RETRY_S, max(0.0, deadline - time.monotonic())))
                continue
            if response.status_code != 200:
                raise RuntimeError(f"qwen-asr-service request failed: HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise RuntimeError("qwen-asr-service returned a malformed response")
            return data

    def _result_from(self, data: dict, *, source: str) -> ASRResult:
        text = str(data.get("text") or "").strip()
        language = data.get("detected_language")
        metadata: dict[str, object] = {"source": source}
        if isinstance(data.get("decode_elapsed_s"), (int, float)):
            metadata["decode_elapsed_s"] = float(data["decode_elapsed_s"])
        return ASRResult(
            text=text,
            confidence=None,
            english_ratio=_estimate_english_ratio(text),
            model_name=str(data.get("model") or self.provider_name),
            detected_language=str(language).strip() or None if language is not None else None,
            metadata=metadata,
        )

    # -- file path -----------------------------------------------------------

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        if not wav_path.exists():
            raise FileNotFoundError(wav_path)
        data = self._post_transcribe(wav_path.read_bytes(), hotwords, timeout_s=self.timeout_s)
        return self._result_from(data, source="qwen_asr_service")

    # -- realtime session ----------------------------------------------------

    def start_realtime_session(
        self,
        *,
        hotwords: list[str],
        prefix_context: str = "",
        cancel_event: threading.Event | None = None,
    ) -> _QwenServiceRealtimeSession:
        # ``prefix_context`` is accepted for caller compatibility and
        # deliberately ignored, exactly as in ConfuciusASRProvider: the
        # ASR context is a recognition prompt, not continuation history,
        # and injecting committed text makes decoders replay it.
        del prefix_context
        return _QwenServiceRealtimeSession(self, hotwords, cancel_event=cancel_event)


class _QwenServiceRealtimeSession:
    """Buffer one press; preview by throttled re-decode; finish decodes all."""

    def __init__(
        self,
        provider: QwenServiceASRProvider,
        hotwords: list[str],
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._provider = provider
        self._hotwords = list(hotwords)
        self._cancel_event = cancel_event
        self._pcm = bytearray()
        self._lock = threading.Lock()
        self._last_text = ""
        self._cancelled = False
        self._finished = False
        self._finishing = False
        self._started_at = time.perf_counter()
        self._preview_thread: threading.Thread | None = None
        self._preview_wake = threading.Event()
        self._last_preview_started = 0.0
        self._last_preview_covered_s = 0.0

    # -- audio intake --------------------------------------------------------

    def push_audio(self, raw: bytes) -> dict[str, str]:
        if raw:
            pcm = f32le_to_pcm16le(raw)
            with self._lock:
                if self._cancelled or self._finished:
                    return {"text": self._last_text}
                self._pcm.extend(pcm)
        self._maybe_start_preview()
        with self._lock:
            return {"text": self._last_text}

    def _audio_seconds_locked(self) -> float:
        return len(self._pcm) / 2.0 / SAMPLE_RATE

    # -- preview machinery -----------------------------------------------------

    def _maybe_start_preview(self) -> None:
        if self._preview_thread is not None and self._preview_thread.is_alive():
            self._preview_wake.set()
            return
        if self._cancelled or self._finishing:
            return
        thread = threading.Thread(target=self._preview_loop, name="qwen-service-preview", daemon=True)
        self._preview_thread = thread
        thread.start()

    def _preview_loop(self) -> None:
        while True:
            self._preview_wake.wait(timeout=0.25)
            self._preview_wake.clear()
            with self._lock:
                if self._cancelled or self._finishing or self._finished:
                    return
                total_s = self._audio_seconds_locked()
                new_s = total_s - self._last_preview_covered_s
                due = (
                    total_s <= self._provider.preview_max_audio_s
                    and new_s >= self._provider.preview_min_new_s
                    and (time.monotonic() - self._last_preview_started) >= self._provider.preview_interval_s
                )
                snapshot = bytes(self._pcm) if due else None
            if snapshot is None:
                if self._cancelled or self._finishing:
                    return
                continue
            self._last_preview_started = time.monotonic()
            covered_s = len(snapshot) / 2.0 / SAMPLE_RATE
            try:
                data = self._provider._post_transcribe(
                    pcm16le_to_wav_bytes(snapshot),
                    self._hotwords,
                    timeout_s=self._provider.preview_timeout_s,
                )
            except Exception:  # noqa: BLE001 - previews are best-effort by design
                logger.debug("qwen-asr-service preview decode failed", exc_info=True)
                continue
            with self._lock:
                if self._cancelled or self._finishing or self._finished:
                    return
                self._last_preview_covered_s = covered_s
                text = str(data.get("text") or "").strip()
                if text:
                    self._last_text = text

    # -- final / cancel --------------------------------------------------------

    def finish(self) -> ASRResult:
        with self._lock:
            if self._cancelled or (self._cancel_event is not None and self._cancel_event.is_set()):
                raise RuntimeError("qwen-asr-service session was cancelled")
            if self._finished:
                raise RuntimeError("qwen-asr-service session already finished")
            self._finished = True
            self._finishing = True
            pcm = bytes(self._pcm)
        self._preview_wake.set()
        if not pcm:
            return ASRResult(text="", model_name=self._provider.provider_name,
                             metadata={"source": "qwen_asr_service"})
        chunk_bytes = int(_FINISH_CHUNK_S * SAMPLE_RATE) * 2
        texts: list[str] = []
        language: str | None = None
        model_name = self._provider.provider_name
        decode_total = 0.0
        for offset in range(0, len(pcm), chunk_bytes):
            # 取消后不再占用单槽服务去解码剩余分块。
            if self._cancelled or (self._cancel_event is not None and self._cancel_event.is_set()):
                raise RuntimeError("qwen-asr-service session was cancelled")
            data = self._provider._post_transcribe(
                pcm16le_to_wav_bytes(pcm[offset : offset + chunk_bytes]),
                self._hotwords,
                timeout_s=self._provider.timeout_s,
            )
            texts.append(str(data.get("text") or ""))
            if data.get("detected_language"):
                language = str(data["detected_language"])
            if data.get("model"):
                model_name = str(data["model"])
            if isinstance(data.get("decode_elapsed_s"), (int, float)):
                decode_total += float(data["decode_elapsed_s"])
        text = "".join(texts).strip()
        return ASRResult(
            text=text,
            confidence=None,
            english_ratio=_estimate_english_ratio(text),
            model_name=model_name,
            detected_language=language,
            metadata={"source": "qwen_asr_service", "decode_elapsed_s": round(decode_total, 3)},
        )

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._pcm.clear()
        self._preview_wake.set()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._started_at) * 1000.0
