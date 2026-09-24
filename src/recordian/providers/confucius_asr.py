"""Confucius4-R2T2 (NetEase Youdao) streaming ASR provider over WebSocket.

Protocol reference: official ``ws_server.py`` / ``ws_client.py`` pinned at
commit ``c4611929bc3592b38dab34e96a8c9940d6da3755`` (verified against the
vendored copy in ``../runtime/Confucius4-R2T2``).

Wire contract (endpoint ``/asr_stream_api_v1``):

- Client sends a JSON header first; ``requestId`` is required
  (``validate_header``). Auth is the ``secret_key`` field; the server closes
  with code 4401 when it is wrong (no error JSON). ``system_prompt``
  (hotwords/context) is capped server-side at 4000 chars
  (``resolve_system_prompt`` / ``resolve_qwen_context``).
- Server answers ``{"status": "connected", ...}`` before any audio.
- Audio frames are binary PCM16 (int16 LE), 16 kHz, mono.
- Server pushes ``{"status": "success", "msg": {"text": ..., "reset": ...}}``
  incrementally; ``text`` is the *new* text since the previous message and
  must be concatenated client-side. ``reset: true`` marks a segment boundary
  (VAD end / hallucination), it does NOT clear the accumulated transcript.
  Empty ``{}`` frames are keepalives.
- Client ends the stream with the text frame ``YOUDAO_ONETIME_ASR_STREAM_EOS``.
  The server first drains every audio frame queued ahead of EOS — emitting
  further deltas and possibly ordinary segment resets — and only then emits
  the final ``{"status": "success", "msg": {"text", "reset": true}}`` and
  closes the connection (normal close, code 1000).

Termination contract (strongest observable, since upstream v1 has no unique
final-message type): a session ends successfully ONLY when the server sent a
clean CLOSE frame with code 1000 AND the LAST application data frame before
that close was a post-EOS ``reset: true`` success message. An ordinary reset
followed by any further application frame (delta, keepalive ``{}``) is a
segment boundary, not the final — it is invalidated and the session must
fail. Wire-identical faulty servers (e.g. an in-flight ordinary reset that
happens to be the last frame before a 1000 close with no EOS processing at
all) cannot be distinguished from a fast healthy final, and this client does
not claim to. Anything else (error status, malformed payload, missing final,
close code != 1000, dropped connection, timeout) is an explicit failure —
never a silent success.

Optional dependencies: ``websocket-client`` (imported lazily when a session
starts). Audio conversion uses only the standard library, so importing this
module never requires numpy.
"""

from __future__ import annotations

import array
import json
import logging
import math
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import urlparse, urlunparse

from ..models import ASRResult
from .asr_context import ASRContextComposer
from .base import ASRProvider, ASRProviderCapabilities, _estimate_english_ratio

logger = logging.getLogger(__name__)

EOS_MESSAGE = "YOUDAO_ONETIME_ASR_STREAM_EOS"
DEFAULT_WS_PATH = "/asr_stream_api_v1"
SAMPLE_RATE = 16000
# Server-side cap (resolve_system_prompt in the pinned ws_server.py).
MAX_SYSTEM_PROMPT_CHARS = 4000
# Official streaming step (CHUNK_ASR_SECONDS in ws_server.py). The provider
# declares it so the worker feeds 160 ms frames instead of its 0.5 s default.
REALTIME_CHUNK_SIZE_SEC = 0.16
# Bounded send queue: at the official 160 ms step this caps realtime
# backpressure at ~1 s of audio. File transcription uses blocking enqueue
# (backpressure) on the same queue instead of failing.
_SEND_QUEUE_MAX_FRAMES = 6
_SEND_CHUNK_SAMPLES = int(SAMPLE_RATE * REALTIME_CHUNK_SIZE_SEC)

# RFC 6455 opcodes / close codes (matched against websocket.ABNF values).
_OPCODE_TEXT = 0x1
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_CLOSE_NORMAL = 1000
_CLOSE_NO_STATUS = 1005
_CLOSE_ABNORMAL = 1006
_CLOSE_UNAUTHORIZED = 4401  # server-side auth failure (secret_key rejected)

# Hard ceiling for thread joins on close/cancel — one shared budget, never
# several stacked waits.
_CLOSE_JOIN_BUDGET_S = 1.0
_FFMPEG_TIMEOUT_S = 120.0
# Server-provided text embedded into our errors is truncated; we never echo
# request payloads, credentials, or full server bodies.
_MAX_SERVER_MSG_CHARS = 160


class ConfuciusProtocolError(RuntimeError):
    """The server violated the pinned protocol (handshake, EOF, close code)."""


def _truncate(text: str, limit: int = _MAX_SERVER_MSG_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _strip_userinfo_netloc(parsed) -> str:
    """Rebuild netloc without any userinfo (credentials never appear in URLs)."""
    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def sanitize_ws_url(url: str) -> str:
    """Return the URL without userinfo/query/fragment — safe for diagnostics."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(netloc=_strip_userinfo_netloc(parsed), query="", fragment=""))


def resolve_ws_endpoint(endpoint: str) -> str:
    """Normalize an endpoint into a ``ws://`` URL with the default API path.

    Accepts ``ws://``/``wss://`` directly, and ``http(s)://`` as a convenience
    (translated to ws/wss). An empty path gets ``/asr_stream_api_v1``.
    Userinfo, query and fragment are stripped — credentials must go through
    ``asr_api_key`` (the protocol ``secret_key``), never through the URL.
    """
    raw = str(endpoint or "").strip()
    if not raw:
        raise ValueError("confucius-asr requires asr_realtime_endpoint (ws://host:port/asr_stream_api_v1)")
    if "://" not in raw:
        raw = f"ws://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
    if scheme not in {"ws", "wss"}:
        raise ValueError(f"confucius-asr endpoint must be ws(s)://, got scheme {parsed.scheme!r}")
    netloc = _strip_userinfo_netloc(parsed)
    if not netloc:
        raise ValueError(f"confucius-asr endpoint is missing host: {sanitize_ws_url(raw)!r}")
    path = parsed.path if parsed.path not in {"", "/"} else DEFAULT_WS_PATH
    return urlunparse(parsed._replace(scheme=scheme, netloc=netloc, path=path, params="", query="", fragment=""))


def compose_system_prompt(
    base_context: str,
    hotwords: list[str],
    *,
    max_chars: int = MAX_SYSTEM_PROMPT_CHARS,
    max_hotwords: int = 40,
) -> str:
    """Build the ``system_prompt`` header value within the server budget.

    Hotwords are kept whole; when the budget is tight the free-form base
    context is shortened first (never a blind ``[:80]`` cut), and only then
    are trailing hotwords dropped.
    """
    composer = ASRContextComposer(base_context, max_hotwords=max_hotwords)
    normalized = composer.normalize_hotwords(hotwords)
    hotword_line = "、".join(normalized)
    base = composer.base_context
    if hotword_line:
        full = f"{base}\n热词参考: {hotword_line}" if base else f"热词参考: {hotword_line}"
    else:
        full = base
    if len(full) <= max_chars:
        return full
    if hotword_line:
        hotword_part = f"热词参考: {hotword_line}"
        room = max_chars - len(hotword_part) - 1
        if room > 0:
            return f"{base[:room]}\n{hotword_part}"
        # Even hotwords alone exceed the budget: keep as many leading terms as fit.
        kept: list[str] = []
        used = len("热词参考: ")
        for term in normalized:
            extra = len(term) + (1 if kept else 0)
            if used + extra > max_chars:
                break
            kept.append(term)
            used += extra
        return "热词参考: " + "、".join(kept) if kept else ""
    return base[:max_chars]


def f32le_to_pcm16le(raw: bytes) -> bytes:
    """Convert f32le/16k/mono worker audio to PCM16 LE wire bytes.

    Standard-library only (no numpy). Sanitizes non-finite values and clips
    to [-1, 1] so a bad frame can never poison the stream. ``raw`` must be a
    whole number of float32 samples.
    """
    if not raw:
        return b""
    if len(raw) % 4 != 0:
        raise ValueError(f"f32le audio must be a multiple of 4 bytes, got {len(raw)}")
    samples = struct.unpack(f"<{len(raw) // 4}f", raw)
    pcm = array.array("h")
    append = pcm.append
    for value in samples:
        if not math.isfinite(value):
            value = 0.0 if math.isnan(value) else (1.0 if value > 0 else -1.0)
        elif value > 1.0:
            value = 1.0
        elif value < -1.0:
            value = -1.0
        scaled = value * 32767.0
        append(int(scaled + 0.5) if scaled >= 0 else int(scaled - 0.5))
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm.tobytes()


def wav_file_to_pcm16le(path: Path) -> bytes:
    """Read a WAV file as PCM16 LE; convert explicitly when the format differs.

    Non-WAV input (e.g. OGG) is never reinterpreted as raw PCM — it is either
    converted via ffmpeg (with a bounded, explainable timeout) or rejected
    with an explicit error.
    """
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                channels = handle.getnchannels()
                rate = handle.getframerate()
                width = handle.getsampwidth()
                if channels == 1 and rate == SAMPLE_RATE and width == 2:
                    frames = handle.readframes(handle.getnframes())
                    # WAV PCM16 is little-endian by definition.
                    return frames
        except wave.Error:
            pass  # fall through to explicit conversion
    ffmpeg_bin = which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError(
            f"audio file {path} is not 16 kHz mono PCM16 WAV and ffmpeg is unavailable; "
            "convert it explicitly (ffmpeg -i in -ac 1 -ar 16000 -f s16le) or provide such a WAV"
        )
    try:
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
            timeout=_FFMPEG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg conversion of {path} timed out after {_FFMPEG_TIMEOUT_S:.0f}s; "
            "convert it explicitly (ffmpeg -i in -ac 1 -ar 16000 -f s16le) and provide the WAV"
        ) from exc
    if proc.returncode != 0:
        detail = _truncate(proc.stderr.decode("utf-8", errors="replace"), 200)
        raise RuntimeError(
            f"failed to convert audio {path} to 16 kHz mono PCM16 "
            f"(ffmpeg exit code {proc.returncode}): {detail or 'no ffmpeg diagnostics'}"
        )
    return proc.stdout


def _parse_close_code(data: Any) -> int:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    if isinstance(data, (bytes, bytearray)) and len(data) >= 2:
        return int(struct.unpack("!H", bytes(data[:2]))[0])
    return _CLOSE_NO_STATUS


class ConfuciusRealtimeSession:
    """One streaming session: decoupled sender/receiver threads, bounded queue.

    ``push_audio`` never waits for a model inference; it converts the f32le
    worker frame to PCM16, enqueues it, and returns the current accumulated
    transcript snapshot. The receiver thread is the only place that mutates
    the accumulation, so late packets after cancel are discarded.

    Termination contract (verified against the pinned server): after the
    client sends EOS, the server drains all queued audio — further deltas and
    ordinary segment resets keep arriving — then sends one final reset
    message and closes with code 1000. The receiver therefore keeps
    accumulating until it observes the actual CLOSE frame; ``finish()``
    succeeds only when the LAST application data frame before a clean close
    1000 was a post-EOS reset. Since v1 has no unique final-message type,
    this is the strongest observable contract; wire-identical faulty servers
    are explicitly out of scope (see the module docstring).
    """

    def __init__(
        self,
        *,
        ws_url: str,
        api_key: str | None,
        language: str,
        system_prompt: str,
        timeout_s: float,
        use_vad: bool,
        ws_module: Any | None = None,
    ) -> None:
        if ws_module is None:
            try:
                import websocket as ws_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise ImportError(
                    "websocket-client is required for confucius-asr. "
                    "Install with: pip install -e '.[confucius-asr]'"
                ) from exc
        self._ws_module = ws_module
        self._ws_url = ws_url
        self._api_key = api_key
        self._language = language
        self._system_prompt = system_prompt
        self._timeout_s = max(1.0, float(timeout_s))
        self._use_vad = use_vad

        self._ws: Any | None = None
        self._send_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_SEND_QUEUE_MAX_FRAMES)
        self._lock = threading.Lock()
        self._transcript_parts: list[str] = []
        self._segments = 0
        self._error: str | None = None
        # Armed immediately BEFORE the EOS frame is written to the socket (a
        # fast server can answer the final reset while send() is still
        # returning) — never conflated with "finish() was called".
        self._eos_sent = False
        # Not sticky: True only while the latest application data frame is a
        # post-EOS reset; any later delta or keepalive sets it back to False.
        self._final_received = False
        self._close_code: int | None = None
        self._closed = False
        self._finished = False
        self._cancelled = False
        self._final_result: ASRResult | None = None
        self._done = threading.Event()
        self._receiver_thread: threading.Thread | None = None
        self._sender_thread: threading.Thread | None = None
        self._started_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        request_id = uuid.uuid4().hex
        header: dict[str, Any] = {
            "requestId": request_id,
            "channels": 1,
            "sample_rate": SAMPLE_RATE,
            "language": self._language or "zhen",
            "use_vad": bool(self._use_vad),
            "mode": "slow",
        }
        if self._api_key:
            header["secret_key"] = self._api_key
        if self._system_prompt:
            header["system_prompt"] = self._system_prompt
        self._request_id = request_id

        try:
            ws = self._ws_module.create_connection(self._ws_url, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001
            # The exception text may embed the full URL (incl. credentials) —
            # only the type name is safe to surface.
            raise ConfuciusProtocolError(
                f"confucius-asr cannot connect to {self._safe_url()}: {type(exc).__name__}"
            ) from exc
        self._ws = ws
        self._started_at = time.perf_counter()
        try:
            ws.send(json.dumps(header, ensure_ascii=False))
            greeting = self._recv_handshake()
        except Exception:
            self._force_close()
            raise
        status = greeting.get("status")
        if status != "connected":
            # The greeting's msg field is server-controlled text — do not echo
            # it (it may carry transcript/credential material).
            self._force_close()
            raise ConfuciusProtocolError(
                f"confucius-asr handshake rejected by {self._safe_url()}: "
                f"expected status 'connected', got {_truncate(str(status), 40)!r}"
            )

        self._receiver_thread = threading.Thread(
            target=self._receiver_loop, name="confucius-asr-recv", daemon=True
        )
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="confucius-asr-send", daemon=True
        )
        self._receiver_thread.start()
        self._sender_thread.start()

    def _safe_url(self) -> str:
        return sanitize_ws_url(self._ws_url)

    def _recv_handshake(self) -> dict[str, Any]:
        assert self._ws is not None
        ws = self._ws
        try:
            opcode, frame = ws.recv_data_frame(control_frame=True)
        except Exception as exc:  # noqa: BLE001
            raise ConfuciusProtocolError(
                f"confucius-asr handshake failed: server closed before 'connected' "
                f"({type(exc).__name__})"
            ) from exc
        if opcode == _OPCODE_CLOSE:
            code = _parse_close_code(getattr(frame, "data", b""))
            hint = " (unauthorized: secret_key rejected)" if code == _CLOSE_UNAUTHORIZED else ""
            raise ConfuciusProtocolError(
                f"confucius-asr handshake rejected by {self._safe_url()}: "
                f"server closed with code {code}{hint}"
            )
        raw = getattr(frame, "data", b"")
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = bytes(raw).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConfuciusProtocolError(
                    "confucius-asr handshake failed: greeting is not valid UTF-8"
                ) from exc
        if not isinstance(raw, str) or not raw.strip():
            raise ConfuciusProtocolError("confucius-asr handshake failed: empty greeting frame")
        try:
            greeting = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfuciusProtocolError(
                "confucius-asr handshake failed: greeting is not JSON"
            ) from exc
        if not isinstance(greeting, dict):
            raise ConfuciusProtocolError("confucius-asr handshake failed: greeting is not a JSON object")
        return greeting

    # -- sender ------------------------------------------------------------

    def _sender_loop(self) -> None:
        while True:
            frame = self._send_queue.get()
            if frame is None:
                return
            ws = self._ws
            if ws is None:
                return
            try:
                ws.send_binary(frame)
            except Exception as exc:  # noqa: BLE001
                self._set_error(f"send failed: {type(exc).__name__}")
                return

    def _enqueue(self, frame: bytes | None, *, block: bool, timeout: float | None = None) -> None:
        try:
            if block:
                self._send_queue.put(frame, block=True, timeout=timeout)
            else:
                self._send_queue.put(frame, block=False)
        except queue.Full as exc:
            if block:
                # Caller's budget exhausted: backpressure signal, not a
                # session-fatal overflow — the caller decides to retry/abort.
                raise TimeoutError(
                    "confucius-asr send queue did not drain before the deadline"
                ) from exc
            # Realtime path: frames would be silently dropped — the session
            # is broken and must fail explicitly on the next call/finish.
            self._set_error("send queue overflow: server is not consuming audio fast enough")
            raise RuntimeError(
                "confucius-asr send queue overflow — audio would be silently dropped; failing explicitly"
            ) from exc

    # -- receiver ----------------------------------------------------------

    def _is_read_timeout(self, exc: BaseException) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        wte = getattr(self._ws_module, "WebSocketTimeoutException", None)
        return wte is not None and isinstance(exc, wte)

    def _receiver_loop(self) -> None:
        try:
            while True:
                with self._lock:
                    stopping = self._cancelled or self._closed
                if stopping:
                    return
                ws = self._ws
                if ws is None:
                    return
                try:
                    opcode, frame = ws.recv_data_frame(control_frame=True)
                except Exception as exc:  # noqa: BLE001
                    if self._is_read_timeout(exc):
                        # The read blocked for one socket-timeout window; loop
                        # to re-check state. This never busy-loops.
                        continue
                    with self._lock:
                        stopping = self._cancelled or self._closed or self._ws is not ws
                    if stopping:
                        return
                    with self._lock:
                        if self._close_code is None:
                            self._close_code = _CLOSE_ABNORMAL
                    self._set_error(
                        f"connection dropped without a CLOSE frame "
                        f"(treated as {_CLOSE_ABNORMAL}): {type(exc).__name__}"
                    )
                    return
                data = getattr(frame, "data", b"")
                if opcode == _OPCODE_CLOSE:
                    with self._lock:
                        self._close_code = _parse_close_code(data)
                    return
                if opcode == _OPCODE_PING:
                    try:
                        ws.pong(bytes(data) if isinstance(data, (bytes, bytearray)) else b"")
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if opcode != _OPCODE_TEXT:
                    continue
                self._handle_text_frame(data)
                with self._lock:
                    if self._error is not None or self._cancelled:
                        return
        finally:
            self._done.set()

    def _handle_text_frame(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            try:
                raw = bytes(data).decode("utf-8")
            except UnicodeDecodeError:
                self._set_error("malformed payload: server frame is not valid UTF-8")
                return
        elif isinstance(data, str):
            raw = data
        else:
            self._set_error("malformed payload: unexpected frame data type")
            return
        text_frame = raw.strip()
        if not text_frame:
            return  # empty keepalive frame
        try:
            payload = json.loads(text_frame)
        except json.JSONDecodeError:
            self._set_error("malformed payload: server frame is not JSON")
            return
        if not isinstance(payload, dict):
            self._set_error("malformed payload: server frame is not a JSON object")
            return
        status = payload.get("status")
        if status is None:
            # {} keepalive / ack frames. A keepalive is application data that
            # is NOT the EOS final: if it arrives after a post-EOS reset, that
            # reset was an ordinary segment boundary — invalidate it.
            with self._lock:
                self._final_received = False
            return
        if status == "error":
            # Never echo the server-provided detail into public errors: it may
            # carry transcript or credential material. Keep only the safe
            # category (and a length for support correlation).
            detail = payload.get("msg")
            length = len(str(detail)) if detail is not None else 0
            self._set_error(
                f"server reported error status (detail {length} chars, content not echoed)"
            )
            return
        if status != "success":
            self._set_error(f"unexpected server status: {_truncate(str(status), 40)!r}")
            return
        msg = payload.get("msg")
        if not isinstance(msg, dict):
            self._set_error("malformed payload: 'msg' is not an object")
            return
        piece = msg.get("text", "")
        if piece is None:
            piece = ""
        if not isinstance(piece, str):
            self._set_error("malformed payload: 'text' is not a string")
            return
        is_reset = bool(msg.get("reset", False))
        with self._lock:
            if self._cancelled:
                return  # discard late packets after cancel
            if piece:
                self._transcript_parts.append(piece)
            if is_reset:
                self._segments += 1
                if self._eos_sent:
                    # Candidate EOS final. NOT sticky: the receiver must keep
                    # accumulating until the server's CLOSE frame, and any
                    # later application data frame (delta or keepalive)
                    # invalidates this — only a post-EOS reset that stays the
                    # LAST data frame before a clean 1000 close is the final.
                    self._final_received = True
            else:
                # A non-reset delta proves the earlier reset was an ordinary
                # segment boundary, not the EOS final.
                self._final_received = False

    def _set_error(self, message: str) -> None:
        with self._lock:
            if self._error is None:
                self._error = message
        self._done.set()

    def _check_error(self) -> None:
        with self._lock:
            error = self._error
            cancelled = self._cancelled
        if cancelled:
            raise RuntimeError("confucius-asr session was cancelled")
        if error:
            raise RuntimeError(f"confucius-asr stream failed: {error}")

    # -- public session API -------------------------------------------------

    def push_audio(self, raw: bytes) -> dict[str, str]:
        """Convert one f32le frame and enqueue it; return the current snapshot.

        Realtime path: non-blocking enqueue — when the backlog cap (~1 s of
        audio) is exceeded this fails explicitly instead of silently dropping.
        """
        if self._finished:
            raise RuntimeError("confucius-asr session already finished")
        self._check_error()
        pcm = f32le_to_pcm16le(raw)
        if pcm:
            self._enqueue(pcm, block=False)
        with self._lock:
            text = "".join(self._transcript_parts)
        return {"text": text}

    def push_pcm16(self, pcm: bytes, *, block: bool = False, timeout_s: float | None = None) -> dict[str, str]:
        """Enqueue wire-ready PCM16 bytes (used by file transcription).

        With ``block=True`` this applies bounded backpressure: the call waits
        up to ``timeout_s`` for queue room and raises ``TimeoutError`` when
        the budget is exhausted.
        """
        if self._finished:
            raise RuntimeError("confucius-asr session already finished")
        self._check_error()
        if len(pcm) % 2 != 0:
            raise ValueError(f"PCM16 audio must be a multiple of 2 bytes, got {len(pcm)}")
        if pcm:
            self._enqueue(pcm, block=block, timeout=timeout_s)
        with self._lock:
            text = "".join(self._transcript_parts)
        return {"text": text}

    def finish(self) -> ASRResult:
        """Send EOS and wait for the final message plus a clean CLOSE (1000).

        Success requires the strongest observable termination contract of the
        v1 wire protocol: the LAST application data frame before a clean
        CLOSE 1000 is a post-EOS ``reset: true`` success message. Everything
        runs on one absolute monotonic deadline (``timeout_s``) — the
        enqueue, sender drain, EOS wait and close check share the budget; it
        never stacks several independent waits. Idempotent.
        """
        with self._lock:
            if self._final_result is not None:
                return self._final_result
            if self._cancelled:
                raise RuntimeError("confucius-asr finish() after cancel()")
            if self._finished:
                raise RuntimeError("confucius-asr finish() already in progress")
            self._finished = True

        deadline = time.monotonic() + self._timeout_s

        def _remaining() -> float:
            return deadline - time.monotonic()

        try:
            self._check_error()
            # Flush remaining audio frames ahead of EOS, then send EOS itself.
            self._enqueue(None, block=True, timeout=max(0.0, _remaining()))
            sender = self._sender_thread
            if sender is not None:
                sender.join(timeout=max(0.0, _remaining()))
                if sender.is_alive():
                    raise TimeoutError("confucius-asr sender did not drain before EOS")
            ws = self._ws
            if ws is None:
                raise ConfuciusProtocolError("confucius-asr connection lost before EOS")
            # Arm BEFORE the write: on a fast server the final reset and the
            # CLOSE frame can be received (on the receiver thread) while
            # send() is still returning. Arming after the write would
            # deterministically misjudge that final as a pre-EOS ordinary
            # reset and fail a healthy session with "missing EOF".
            with self._lock:
                self._eos_sent = True
            try:
                ws.send(EOS_MESSAGE)
            except Exception as exc:  # noqa: BLE001
                # A send error always fails the session, on every path.
                self._set_error(f"failed to send EOS: {type(exc).__name__}")
                raise ConfuciusProtocolError(
                    f"confucius-asr failed to send EOS: {type(exc).__name__}"
                ) from exc
            remaining = _remaining()
            if remaining <= 0 or not self._done.wait(timeout=remaining):
                raise TimeoutError(
                    f"confucius-asr server did not finish within {self._timeout_s:.0f}s after EOS"
                )
            self._check_error()
            with self._lock:
                final_received = self._final_received
                close_code = self._close_code
                text = "".join(self._transcript_parts)
                segments = self._segments
            if not final_received:
                raise ConfuciusProtocolError(
                    "confucius-asr stream ended without the final post-EOS reset "
                    "as the last data frame before close (missing EOF)"
                )
            if close_code != _CLOSE_NORMAL:
                raise ConfuciusProtocolError(
                    f"confucius-asr server closed with code {close_code}, "
                    f"expected {_CLOSE_NORMAL} (normal close)"
                )
            # Final guard: a late sender error must never become a success.
            self._check_error()
            result = ASRResult(
                text=text,
                confidence=None,
                english_ratio=_estimate_english_ratio(text),
                model_name="confucius-asr",
                detected_language=None,
                metadata={
                    "source": "confucius-asr",
                    "realtime": True,
                    "segments": segments,
                },
            )
            with self._lock:
                self._final_result = result
            return result
        finally:
            self._close()

    def cancel(self) -> None:
        """Abort the session; late server packets are discarded. Idempotent.

        Bounded by one shared join budget — never several stacked waits.
        """
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
        self._close()

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        deadline = time.monotonic() + _CLOSE_JOIN_BUDGET_S
        try:
            self._send_queue.put_nowait(None)
        except queue.Full:
            pass
        self._force_close()
        for thread in (self._sender_thread, self._receiver_thread):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if thread is not None and thread.is_alive():
                thread.join(timeout=remaining)
        self._done.set()

    def _force_close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            # Bounded close handshake — cancel must not stall on a server
            # that never answers the CLOSE frame.
            ws.close(timeout=0.2)
        except TypeError:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    @property
    def elapsed_ms(self) -> float:
        if self._started_at <= 0.0:
            return 0.0
        return (time.perf_counter() - self._started_at) * 1000.0


class ConfuciusASRProvider(ASRProvider):
    """Streaming Confucius4-R2T2 provider (``asr_provider=confucius-asr``).

    The endpoint comes from ``asr_realtime_endpoint``; ``asr_api_key`` is
    reused as the protocol ``secret_key`` and is never logged.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        language: str = "",
        context: str = "",
        use_vad: bool = False,
        ws_module: Any | None = None,
    ) -> None:
        self.endpoint = resolve_ws_endpoint(endpoint)
        self.api_key = (api_key or "").strip() or None
        self.timeout_s = max(1.0, float(timeout_s))
        self.language = language.strip()
        self.context = context.strip()
        self.use_vad = bool(use_vad)
        self._ws_module = ws_module
        # Official 160 ms streaming step; the worker reads this attribute
        # (realtime_asr.py) instead of falling back to its 0.5 s default.
        self.realtime_chunk_size_sec = REALTIME_CHUNK_SIZE_SEC

    @property
    def provider_name(self) -> str:
        return "confucius-asr"

    @property
    def capabilities(self) -> ASRProviderCapabilities:
        return ASRProviderCapabilities(
            supports_hotwords=True,
            supports_context=True,
            supports_language_hint=True,
            supports_realtime=True,
        )

    def _header_language(self) -> str:
        lang = self.language
        if not lang or lang.lower() in {"auto", "zhen", "detect"}:
            return "zhen"  # server maps "zhen" to auto-detect (None)
        return lang

    def start_realtime_session(self, *, hotwords: list[str]) -> ConfuciusRealtimeSession:
        session = ConfuciusRealtimeSession(
            ws_url=self.endpoint,
            api_key=self.api_key,
            language=self._header_language(),
            system_prompt=compose_system_prompt(self.context, hotwords),
            timeout_s=self.timeout_s,
            use_vad=self.use_vad,
            ws_module=self._ws_module,
        )
        session.start()
        return session

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        """Feed a file through the same streaming protocol (16 kHz mono PCM16).

        Bounded backpressure: frames are enqueued with blocking puts against
        one absolute deadline (audio duration + one timeout window), so a
        producer that is faster than the socket can never overflow the queue,
        and a stuck server fails explainably instead of hanging forever.
        """
        if not wav_path.exists():
            raise FileNotFoundError(wav_path)
        pcm = wav_file_to_pcm16le(wav_path)
        session = self.start_realtime_session(hotwords=hotwords)
        try:
            duration_s = len(pcm) / 2.0 / SAMPLE_RATE
            deadline = time.monotonic() + duration_s + self.timeout_s
            frame_bytes = _SEND_CHUNK_SAMPLES * 2
            for offset in range(0, len(pcm), frame_bytes):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"confucius-asr file feed exceeded its deadline "
                        f"({duration_s:.1f}s audio + {self.timeout_s:.0f}s response window)"
                    )
                session.push_pcm16(pcm[offset : offset + frame_bytes], block=True, timeout_s=remaining)
            return session.finish()
        except Exception:
            session.cancel()
            raise
