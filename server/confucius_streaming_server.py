"""Confucius4-R2T2 single-user loopback streaming ASR server.

Speaks the pinned v1 wire contract (upstream ws_server.py @ c4611929) that
Recordian's ``confucius-asr`` provider implements: JSON header with
``requestId`` -> ``{"status": "connected"}`` -> binary PCM16 LE 16 kHz mono
frames -> per-chunk ``{"status": "success", "msg": {"text": <fixed delta>,
"reset": false}}`` -> client EOS text frame -> final ``reset: true`` ->
CLOSE 1000.

Deliberate differences from the upstream server (documented, not hidden):
no FireRedVAD (segment end is client EOS), single concurrent session,
loopback bind by default, local token auth (never the repo debug key),
bounded queues and an explicit per-session audio budget.

Model (``r2t2`` / vLLM) and ``websockets`` are imported lazily inside
:func:`main` / :func:`run_server` so unit tests can import this module on a
machine without a GPU or the model stack installed.
"""

from __future__ import annotations

import argparse
import math
import os
import secrets

SAMPLE_RATE = 16000
STEP_MS = 160
LOOKAHEAD_MS = 160
UNFIX_TOKEN_NUM = 1
EOS_MESSAGE = "YOUDAO_ONETIME_ASR_STREAM_EOS"

# Upstream resolve_system_prompt budget (pinned ws_server.py @ c4611929).
MAX_SYSTEM_PROMPT_CHARS = 4000

# One 160 ms PCM16 frame is 5120 bytes; accept up to 4 frames per message so
# file feeding is not rejected, but reject pathological payloads.
MAX_FRAME_BYTES = SAMPLE_RATE * STEP_MS // 1000 * 2 * 4
# Explicit session budget: streaming re-feeds all accumulated audio per chunk,
# so cost grows with duration. 30 s at 16 kHz mono. Not advertised as
# unlimited — clients get an explicit error when the budget is exceeded.
DEFAULT_MAX_SESSION_SECONDS = 30.0
DEFAULT_MAX_QUEUED_FRAMES = 32
DEFAULT_IDLE_TIMEOUT_S = 120.0

CLOSE_UNAUTHORIZED = 4401  # auth failure (matches the pinned server)
CLOSE_BUSY = 4429  # single-user service already has an active session

# Populated by run_server() while serving; None in unit tests.
active_model = None  # type: ignore[assignment]


class HeaderError(ValueError):
    """Client header failed validation; maps to an error frame + CLOSE 1008."""


def resolve_system_prompt(header: dict) -> str:
    """Pinned upstream semantics: ``system_prompt`` must be a string <= 4000."""
    if "system_prompt" not in header:
        return ""
    system_prompt = header["system_prompt"]
    if not isinstance(system_prompt, str):
        raise HeaderError("system_prompt must be a string")
    if len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
        raise HeaderError(
            f"system_prompt must be at most {MAX_SYSTEM_PROMPT_CHARS} characters"
        )
    return system_prompt.strip()


def resolve_context(header: dict) -> str:
    """Upstream resolve_qwen_context: optional smooth prefix + system_prompt."""
    parts = []
    if header.get("smooth"):
        parts.append("Smooth the text")
    system_prompt = resolve_system_prompt(header)
    if system_prompt:
        parts.append(system_prompt)
    return "\n".join(parts)


def resolve_language(header: dict) -> str | None:
    """Header language -> model language; zhen/auto/detect/empty -> None."""
    language = header.get("language", "zhen")
    if language is None:
        return None
    if not isinstance(language, str):
        raise HeaderError("language must be a string")
    if not language.strip() or language.strip().lower() in {"zhen", "auto", "detect"}:
        return None
    return language.strip()


def validate_header(raw: object) -> dict:
    """Validate the first client message; returns the header dict."""
    if not isinstance(raw, dict):
        raise HeaderError("json header is expected")
    request_id = raw.get("requestId")
    if not isinstance(request_id, str) or not request_id.strip():
        raise HeaderError("invalid json header: requestId is required")
    channels = raw.get("channels", 1)
    sample_rate = raw.get("sample_rate", SAMPLE_RATE)
    if channels != 1:
        raise HeaderError(f"only mono audio is supported, got channels={channels!r}")
    if sample_rate != SAMPLE_RATE:
        raise HeaderError(f"only {SAMPLE_RATE} Hz audio is supported, got sample_rate={sample_rate!r}")
    resolve_language(raw)  # raises on a non-string language
    resolve_system_prompt(raw)  # raises on a non-string / overlong prompt
    return raw


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {parsed}")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive finite number, got {parsed}")
    return parsed


def _gpu_util(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 1:
        raise argparse.ArgumentTypeError(f"must be in (0, 1], got {parsed}")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError(f"must be in 0..65535 (0 = ephemeral), got {parsed}")
    return parsed


def validate_config(args) -> None:
    """Reject invalid config at the programmatic boundary (tests and probes
    construct the Namespace directly, bypassing the parser's type checks).
    Runs before any resource (model, queue, socket) is created."""
    problems = []
    if not isinstance(args.max_queued_frames, int) or args.max_queued_frames < 1:
        problems.append(f"max_queued_frames must be >= 1, got {args.max_queued_frames!r}")
    if not isinstance(args.max_session_seconds, (int, float)) \
            or not math.isfinite(args.max_session_seconds) or args.max_session_seconds <= 0:
        problems.append(f"max_session_seconds must be positive finite, got {args.max_session_seconds!r}")
    if not isinstance(args.idle_timeout_s, (int, float)) \
            or not math.isfinite(args.idle_timeout_s) or args.idle_timeout_s <= 0:
        problems.append(f"idle_timeout_s must be positive finite, got {args.idle_timeout_s!r}")
    if not isinstance(args.gpu_memory_utilization, (int, float)) \
            or not math.isfinite(args.gpu_memory_utilization) \
            or not 0 < args.gpu_memory_utilization <= 1:
        problems.append(
            f"gpu_memory_utilization must be in (0, 1], got {args.gpu_memory_utilization!r}")
    if not isinstance(args.max_model_len, int) or args.max_model_len < 256:
        problems.append(f"max_model_len must be an integer >= 256, got {args.max_model_len!r}")
    if not isinstance(args.port, int) or not 0 <= args.port <= 65535:
        problems.append(f"port must be in 0..65535 (0 = ephemeral), got {args.port!r}")
    if problems:
        raise ValueError("invalid server config: " + "; ".join(problems))


def default_token_path() -> str:
    """Per-user config dir (XDG_CONFIG_HOME, else ~/.config/recordian).

    The token is a private secret — it must not default into a project
    working directory where it could be committed."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "recordian", "confucius_server_token.txt")


def load_or_create_token(path: str) -> str:
    """Read a local auth token, or atomically create one with mode 0600.

    Never overwrites an existing file; an empty/whitespace token file is a
    hard error (fail closed). The token is a hex secret, never logged.
    Parent directories are created private (0700) when missing.
    """
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
        if not token:
            raise ValueError(f"token file {path} is empty; refusing to start without auth")
        return token
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    token = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Confucius4-R2T2 single-user loopback streaming ASR server (v1 protocol)"
    )
    parser.add_argument(
        "--model-dir",
        default=os.environ.get("CONFUCIUS_MODEL_DIR", "models/Confucius4-R2T2"),
        help="Local Confucius4-R2T2 checkpoint directory (env: CONFUCIUS_MODEL_DIR)",
    )
    parser.add_argument(
        "--r2t2-source",
        default=os.environ.get("R2T2_SOURCE", ""),
        help="Path to the pinned Confucius4-R2T2 source checkout providing the 'r2t2' "
             "package (env: R2T2_SOURCE). Optional when confucius4-r2t2 is pip-installed.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default loopback)")
    parser.add_argument(
        "--port", type=_port, default=int(os.environ.get("CONFUCIUS_PORT", "8321")),
        help="Bind port (env: CONFUCIUS_PORT, default 8321)",
    )
    parser.add_argument("--gpu-memory-utilization", type=_gpu_util, default=0.80)
    parser.add_argument("--max-model-len", type=_positive_int, default=4096)
    parser.add_argument(
        "--token-file",
        default=os.environ.get("CONFUCIUS_TOKEN_FILE", ""),
        help="Local auth token file, created 0600 if missing (env: CONFUCIUS_TOKEN_FILE). "
             "Default: $XDG_CONFIG_HOME/recordian/confucius_server_token.txt "
             "(or ~/.config/recordian/...) — never inside the project tree.",
    )
    parser.add_argument(
        "--warmup-wav", default="",
        help="Optional public warmup WAV (16 kHz mono). Default: silence warmup.",
    )
    parser.add_argument(
        "--max-session-seconds", type=_positive_float, default=DEFAULT_MAX_SESSION_SECONDS,
        help="Per-session audio budget in seconds (explicit cap, not unlimited)",
    )
    parser.add_argument(
        "--max-queued-frames", type=_positive_int, default=DEFAULT_MAX_QUEUED_FRAMES,
        help="Bounded server-side frame queue; excess closes the session with an error",
    )
    parser.add_argument(
        "--idle-timeout-s", type=_positive_float, default=DEFAULT_IDLE_TIMEOUT_S,
        help="Close the session after this many seconds without any client message",
    )
    parser.add_argument(
        "--ready-file", default="",
        help="Optional ready marker (pid/host/port only, no token); removed on exit",
    )
    return parser


class StreamingSession:
    """One ASR stream; official example.py chunking + adaptive token logic.

    ``model`` is duck-typed (init_streaming_state / streaming_transcribe /
    finish_streaming_transcribe) so tests can inject a fake.
    """

    def __init__(self, model, language: str | None, context: str) -> None:
        self.model = model
        self.state = model.init_streaming_state(
            context=context,
            language=language,
            unfixed_chunk_num=0,
            unfixed_token_num=UNFIX_TOKEN_NUM,
            chunk_size_sec=STEP_MS / 1000.0,
        )
        self.step = SAMPLE_RATE * STEP_MS // 1000
        self.look = SAMPLE_RATE * LOOKAHEAD_MS // 1000
        self.is_first = True
        self.max_new_tokens = max(1, (self.step + self.look) // 1280)
        self.first_max_new_tokens = self.max_new_tokens
        self.floor = min(32, max(4, 2 * (self.step // 1280)))
        self.last_fixed = ""
        self.last_text_tmp = ""
        self.total_new_asr_tokens: list[str] = []
        self.buffer = bytearray()
        self.samples_fed = 0

    def feed(self, pcm_bytes: bytes) -> None:
        self.buffer += pcm_bytes
        self.samples_fed += len(pcm_bytes) // 2

    def _adapt(self, text: str) -> None:
        # Adaptive max_new_tokens, verbatim semantics of upstream example.py.
        last_is_chinese = any(
            "\u4e00" <= ch <= "\u9fff" for ch in (self.total_new_asr_tokens[-1] if self.total_new_asr_tokens else "")
        )
        if len(text) > len(self.last_text_tmp):
            self.last_text_tmp = text
            self.max_new_tokens = max(1, self.step // 1280)
        elif last_is_chinese:
            self.max_new_tokens = max(1, self.step // 1280)
        else:
            self.max_new_tokens += 1
        if last_is_chinese:
            self.max_new_tokens *= 2
        self.max_new_tokens = min(self.floor, self.max_new_tokens)

    def process_ready(self) -> list[tuple[str, float]]:
        """Decode every full chunk in the buffer; returns (delta, cost_ms)."""
        import time

        import numpy as np

        out: list[tuple[str, float]] = []
        while True:
            need = (self.step + self.look if self.is_first else self.step) * 2
            if len(self.buffer) < need:
                return out
            seg_bytes = bytes(self.buffer[:need])
            del self.buffer[:need]
            seg = np.frombuffer(seg_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if self.is_first:
                self.state.chunk_size_sec = (self.step + self.look) / SAMPLE_RATE
                self.state.chunk_size_samples = self.step + self.look
                self.is_first = False
            else:
                self.state.chunk_size_sec = self.step / SAMPLE_RATE
                self.state.chunk_size_samples = self.step
            start = time.perf_counter()
            text, fixed = self.model.streaming_transcribe(seg, self.state, int(self.max_new_tokens))
            cost_ms = (time.perf_counter() - start) * 1000
            fixed = (fixed or "").split("|")[0]
            delta = ""
            if len(fixed) > len(self.last_fixed):
                delta = fixed[len(self.last_fixed):]
                self.last_fixed = fixed
            self._adapt((text or "").split("|")[0])
            out.append((delta, round(cost_ms, 1)))

    def finish(self) -> tuple[str, float]:
        """Flush tail audio; returns (remaining fixed delta, cost_ms)."""
        import time

        import numpy as np

        start = time.perf_counter()
        if self.buffer:
            seg = np.frombuffer(bytes(self.buffer), dtype=np.int16).astype(np.float32) / 32768.0
            self.buffer.clear()
            self.model.streaming_transcribe(seg, self.state, int(self.first_max_new_tokens))
        self.model.finish_streaming_transcribe(self.state, self.first_max_new_tokens)
        cost_ms = (time.perf_counter() - start) * 1000
        final = (self.state.text or "").split("|")[0]
        delta = ""
        if len(final) > len(self.last_fixed):
            delta = final[len(self.last_fixed):]
            self.last_fixed = final
        return delta, round(cost_ms, 1)


def _log(msg: str) -> None:
    import time

    print(f"[confucius-server {time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def _drain_inference(fut) -> None:
    """Wait until an in-flight executor inference has REALLY returned.

    Cancelling the asyncio wrapper does not stop the underlying Python thread
    (threads are not cancellable), so the busy lock must stay held until the
    model call is actually done. Shielding keeps the future alive across
    outer cancellation; the loop re-waits after each cancellation attempt.
    Bounded by the model call itself terminating."""
    import asyncio

    while not fut.done():
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            continue
    if not fut.cancelled():
        fut.exception()  # retrieve so a failed call is not reported as lost


async def _handle_connection(ws, model, token: str, args, busy_lock) -> None:
    """One connection. Never logs transcripts, prompts, tokens or payloads."""
    import asyncio
    import json

    try:
        validate_config(args)
    except ValueError as exc:
        _log(f"invalid config rejected: {exc}")
        try:
            await ws.send(json.dumps({"status": "error", "msg": "server misconfigured"}))
            await ws.close(code=1011, reason="server misconfigured")
        except Exception:  # noqa: BLE001
            pass
        return

    if busy_lock.locked():
        await ws.close(code=CLOSE_BUSY, reason="single-user service busy")
        return
    await busy_lock.acquire()
    request_id = "unknown"
    pending_infer = None
    receiver = None
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        if not isinstance(raw, str):
            raise HeaderError("json header is expected")
        try:
            header = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HeaderError("invalid json header") from exc
        header = validate_header(header)
        request_id = header["requestId"].strip()
        if header.get("secret_key") != token:
            _log(f"requestId={request_id}: unauthorized")
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="Unauthorized")
            return
        language = resolve_language(header)
        context = resolve_context(header)
        _log(f"requestId={request_id}: connected language={language or 'auto'} "
             f"context_chars={len(context)}")
        await ws.send(json.dumps({"status": "connected", "requestId": request_id,
                                  "msg": "", "active_connections": 1}))

        session = StreamingSession(model, language, context)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=args.max_queued_frames)
        max_samples = int(args.max_session_seconds * SAMPLE_RATE)
        # Completion/failure travel OUT-OF-BAND (event + state), never through
        # a data-queue slot: with a full queue an in-band sentinel can only be
        # enqueued by dropping accepted audio — that turned a healthy EOS into
        # silent audio loss. The queue carries PCM16 frames only.
        receiver_done = asyncio.Event()
        terminal = {"kind": "disconnect"}

        async def receiver():
            try:
                async for data in ws:
                    if isinstance(data, str):
                        terminal["kind"] = "eos" if data == EOS_MESSAGE else "bad_text"
                        return
                    try:
                        queue.put_nowait(data)
                    except asyncio.QueueFull:
                        # Explicit backpressure failure, never silent drop.
                        terminal["kind"] = "overflow"
                        return
            finally:
                receiver_done.set()  # never blocks, even on a full queue

        async def next_frame():
            """Next queued audio frame; None once the receiver is done and the
            queue holds nothing more. Cancellation-safe re-arming."""
            import contextlib

            get_task = asyncio.ensure_future(queue.get())
            done_task = asyncio.ensure_future(receiver_done.wait())
            try:
                done, _ = await asyncio.wait(
                    {get_task, done_task}, timeout=args.idle_timeout_s,
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError("idle timeout waiting for client audio")
                if get_task in done:
                    return get_task.result()
                return None
            finally:
                for pending_task in (get_task, done_task):
                    if not pending_task.done():
                        pending_task.cancel()
                        with contextlib.suppress(BaseException):
                            await pending_task

        async def process_frame(data) -> bool:
            """Validate + feed + decode one PCM16 frame. False = session
            already closed with an explicit error; caller must return."""
            nonlocal pending_infer
            if len(data) % 2 != 0 or not data:
                await ws.send(json.dumps({"status": "error",
                                          "msg": "audio frames must be non-empty PCM16 (even byte count)"}))
                await ws.close(code=1008, reason="bad PCM16 frame")
                return False
            if len(data) > MAX_FRAME_BYTES:
                await ws.send(json.dumps({"status": "error", "msg": "audio frame too large"}))
                await ws.close(code=1009, reason="audio frame too large")
                return False
            if session.samples_fed + len(data) // 2 > max_samples:
                await ws.send(json.dumps({
                    "status": "error",
                    "msg": f"session audio budget exceeded ({args.max_session_seconds:.0f}s)"}))
                await ws.close(code=1008, reason="session audio budget exceeded")
                return False
            session.feed(data)
            pending_infer = loop.run_in_executor(None, session.process_ready)
            # shield: cancelling this handler must NOT cancel the future and
            # pretend the model thread stopped — it keeps running either way.
            deltas = await asyncio.shield(pending_infer)
            pending_infer = None
            for delta, cost_ms in deltas:
                await ws.send(json.dumps({
                    "status": "success", "requestId": request_id,
                    "msg": {"text": delta, "reset": False, "asr_cost_ms": cost_ms}},
                    ensure_ascii=False))
            return True

        receiver = asyncio.create_task(receiver())
        while True:
            data = await next_frame()
            if data is None:
                # Receiver finished: drain every frame it accepted BEFORE the
                # terminal state is acted on. Healthy EOS never drops audio.
                while True:
                    try:
                        data = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if not await process_frame(data):
                        return
                break
            if not await process_frame(data):
                return

        kind = terminal["kind"]
        if kind == "overflow":
            await ws.send(json.dumps({
                "status": "error",
                "msg": f"server queue full (max {args.max_queued_frames} frames); "
                       "client is sending faster than realtime"}))
            await ws.close(code=1008, reason="server backpressure")
            return
        if kind == "bad_text":
            await ws.send(json.dumps({"status": "error",
                                      "msg": "unexpected text frame"}))
            await ws.close(code=1008, reason="unexpected text frame")
            return
        if kind == "eos":
            pending_infer = loop.run_in_executor(None, session.finish)
            delta, cost_ms = await asyncio.shield(pending_infer)
            pending_infer = None
            if delta:
                await ws.send(json.dumps({
                    "status": "success", "requestId": request_id,
                    "msg": {"text": delta, "reset": False, "asr_cost_ms": cost_ms}},
                    ensure_ascii=False))
            await ws.send(json.dumps({
                "status": "success", "requestId": request_id,
                "msg": {"text": "", "reset": True}}, ensure_ascii=False))
            _log(f"requestId={request_id}: EOS finish, samples={session.samples_fed}")
        await ws.close(code=1000)
    except HeaderError as exc:
        try:
            await ws.send(json.dumps({"status": "error", "msg": str(exc)}))
            await ws.close(code=1008, reason="invalid request")
        except Exception:  # noqa: BLE001
            pass
        _log(f"requestId={request_id}: rejected ({exc})")
    except Exception as exc:  # noqa: BLE001 - disconnects/timeouts land here
        _log(f"requestId={request_id}: session ended ({type(exc).__name__})")
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if receiver is not None:
            receiver.cancel()
            try:
                # Bounded: cleanup must release the lock even if the receiver
                # misbehaves (its own completion signal is non-blocking).
                await asyncio.wait_for(receiver, timeout=2.0)
            except BaseException:  # noqa: BLE001 - CancelledError/TimeoutError
                pass
        if pending_infer is not None:
            # Model ownership is released only after the old inference has
            # REALLY returned; cancelling this handler never stops the thread.
            await _drain_inference(pending_infer)
        busy_lock.release()


async def _serve(args, model, token: str) -> None:
    import asyncio
    import json
    import signal

    from websockets.asyncio.server import serve

    validate_config(args)  # serve boundary too, not only run_server/handler
    busy_lock = asyncio.Lock()
    ready_file = args.ready_file or ""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    # SIGTERM/SIGINT -> graceful exit so the ready file is removed and the
    # port is released (SIGKILL obviously cannot be cleaned up after).
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-Unix
            pass
    try:
        async with serve(
            lambda ws: _handle_connection(ws, model, token, args, busy_lock),
            args.host,
            args.port,
            ping_interval=None,
        ) as server:
            # Report the socket that was actually bound (args.port may be 0).
            bound_port = server.sockets[0].getsockname()[1]
            _log(f"READY {args.host}:{bound_port}")
            if ready_file:
                with open(ready_file, "w", encoding="utf-8") as handle:
                    json.dump({"pid": os.getpid(), "host": args.host,
                               "port": bound_port}, handle)
            await stop_event.wait()
    finally:
        if ready_file and os.path.exists(ready_file):
            os.unlink(ready_file)


def _warmup(model, warmup_wav: str) -> None:
    """Warm both language paths before READY so first use is the warm path."""
    import numpy as np

    def _run(wav, language) -> None:
        session = StreamingSession(model, language, "")
        pcm = (np.clip(wav, -1.0, 1.0) * 32768.0).astype(np.int16).tobytes()
        session.feed(pcm)
        session.process_ready()
        session.finish()

    # Warm both language paths: forced Chinese and auto-detect (None).
    if warmup_wav:
        import soundfile as sf

        wav, _rate = sf.read(warmup_wav, dtype="float32", always_2d=False)
        if getattr(wav, "ndim", 1) > 1:
            wav = wav.mean(axis=1)
    else:
        wav = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1 s of silence
    for language in ("Chinese", None):
        _run(wav, language)


def load_model(args):
    """Lazy heavy imports; returns the vLLM-backed R2T2 model."""
    if args.r2t2_source:
        import sys

        sys.path.insert(0, args.r2t2_source)
    from r2t2 import R2T2ASRModel

    _suppress_upstream_transcript_prints(R2T2ASRModel)
    return R2T2ASRModel.LLM(
        model=args.model_dir,
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_model_len=args.max_model_len,
        max_num_seqs=1,
        limit_mm_per_prompt={"audio": 1},
        max_new_tokens=4,
        enforce_eager=True,
    )


def _suppress_upstream_transcript_prints(model_cls) -> None:
    """Upstream r2t2_asr.py prints decoded text in finish_streaming_transcribe
    (pinned c4611929, line 577). This server must not log transcripts; wrap the
    method so the print goes nowhere. Upstream source stays untouched."""
    import contextlib
    import io

    original = model_cls.finish_streaming_transcribe

    def quiet_finish(self, state, max_new_tokens=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return original(self, state, max_new_tokens)

    model_cls.finish_streaming_transcribe = quiet_finish


def run_server(args, model=None) -> None:
    """Serve forever. ``model`` may be injected (tests use a fake)."""
    import asyncio

    global active_model  # noqa: PLW0603

    validate_config(args)  # before any resource (token file, model, socket)
    token_file = args.token_file or default_token_path()
    token = load_or_create_token(token_file)
    _log(f"auth token file: {token_file} (value never logged)")
    if model is None:
        _log("loading model ...")
        model = load_model(args)
    active_model = model
    _log("warming up ...")
    _warmup(model, args.warmup_wav)
    asyncio.run(_serve(args, model, token))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run_server(args)


if __name__ == "__main__":
    main()
