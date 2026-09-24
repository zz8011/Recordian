from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO, cast

from .duration_guard import MONITOR_BACKLOG_S
from .linux_commit import resolve_committer, send_hard_enter
from .providers import ASRProvider, HttpCloudProvider, QwenASRProvider
from .remote_paste.client import add_remote_paste_args, resolve_remote_paste_routing, send_remote_paste_from_args
from .runtime_deps import ensure_ffmpeg_available

logger = logging.getLogger(__name__)

# 全局进程注册表
_ACTIVE_PROCESSES: list[subprocess.Popen[Any]] = []

#: 录音 ffmpeg 的 stderr 落盘位置；录音失败（设备忙/被切走）时靠它定位原因。
RECORD_STDERR_LOG_PATH = Path.home() / ".local" / "share" / "recordian" / "record-ffmpeg.log"
_record_stderr_log: BinaryIO | None = None


def _record_stderr_sink() -> BinaryIO | None:
    """懒加载一个进程级共享的 ffmpeg stderr 日志文件（追加写）。"""
    global _record_stderr_log  # noqa: PLW0603
    if _record_stderr_log is not None:
        return _record_stderr_log
    try:
        path = Path(os.environ.get("RECORDIAN_RECORD_LOG", str(RECORD_STDERR_LOG_PATH))).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        _record_stderr_log = open(path, "ab", buffering=0)  # noqa: SIM115
        atexit.register(_close_record_stderr_log)
    except OSError:
        return None
    return _record_stderr_log


def _close_record_stderr_log() -> None:
    global _record_stderr_log  # noqa: PLW0603
    sink, _record_stderr_log = _record_stderr_log, None
    if sink is None:
        return
    try:
        sink.close()
    except OSError:
        pass


def _cleanup_processes() -> None:
    """清理所有活跃进程"""
    for proc in _ACTIVE_PROCESSES[:]:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=0.5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        _ACTIVE_PROCESSES.remove(proc)


# 注册清理函数
atexit.register(_cleanup_processes)


@dataclass(slots=True)
class DictateResult:
    audio_path: str
    record_backend: str
    duration_s: float
    record_latency_ms: float
    transcribe_latency_ms: float
    text: str
    commit: dict[str, object]
    detected_language: str | None = None


@dataclass(slots=True)
class RecordProcessHandle:
    process: subprocess.Popen[Any]
    monitor_stream: BinaryIO | None = None
    monitor_sample_rate: int = 16000
    monitor_channels: int = 1
    monitor_hub: Any | None = None


class MonitorOverflowError(RuntimeError):
    """One reader fell behind the capture pump.

    The pump does not block and does not drop bytes quietly: this reader's
    next drained read raises, and further chunks are not queued for it.
    """


class _ReaderSlot:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self.cv = threading.Condition()
        self.chunks: deque[bytes] = deque()
        self.buffered = 0
        self.eof = False
        self.overflow = False
        self.closed = False


class _MonitorReader:
    def __init__(self, owner: _MonitorFanout, slot: _ReaderSlot) -> None:
        self._owner = owner
        self._slot = slot
        self._buffer = bytearray()
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        want = -1 if size is None else int(size)
        slot = self._slot
        with slot.cv:
            while True:
                while slot.chunks and (want < 0 or len(self._buffer) < want):
                    chunk = slot.chunks.popleft()
                    slot.buffered -= len(chunk)
                    self._buffer.extend(chunk)
                if want >= 0 and len(self._buffer) >= want:
                    break
                if slot.overflow and not self._buffer:
                    raise MonitorOverflowError(
                        f"monitor reader backlog exceeded ({slot.max_bytes} bytes)"
                    )
                if slot.eof or slot.closed or self._closed:
                    break
                if slot.overflow:
                    break
                slot.cv.wait()
        if want < 0 or want > len(self._buffer):
            out = bytes(self._buffer)
            self._buffer.clear()
        else:
            out = bytes(self._buffer[:want])
            del self._buffer[:want]
        if not out and not self._buffer:
            with slot.cv:
                if slot.overflow:
                    raise MonitorOverflowError(
                        f"monitor reader backlog exceeded ({slot.max_bytes} bytes)"
                    )
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        slot = self._slot
        with slot.cv:
            slot.closed = True
            slot.cv.notify_all()
        self._owner.remove_reader(slot)
        self._buffer.clear()


class _MonitorFanout:
    def __init__(
        self,
        source: BinaryIO,
        *,
        chunk_size: int = 4096,
        max_backlog_bytes: int | None = None,
    ) -> None:
        self._source = source
        self._chunk_size = max(1, int(chunk_size))
        # Default is 8 s of 16 kHz mono f32. Callers with a known rate pass
        # the real size; the pump still never blocks on a slow reader.
        if max_backlog_bytes is None:
            max_backlog_bytes = int(MONITOR_BACKLOG_S * 16000 * 4)
        self._max_bytes = max(self._chunk_size, int(max_backlog_bytes))
        self._lock = threading.Lock()
        self._slots: list[_ReaderSlot] = []
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="recordian-monitor-fanout", daemon=True)
        self._thread.start()

    def open_reader(self) -> _MonitorReader:
        slot = _ReaderSlot(self._max_bytes)
        with self._lock:
            if self._closed:
                slot.eof = True
            else:
                self._slots.append(slot)
        return _MonitorReader(self, slot)

    def remove_reader(self, slot: _ReaderSlot) -> None:
        with self._lock:
            if slot in self._slots:
                self._slots.remove(slot)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            slots = list(self._slots)
            self._slots.clear()
        try:
            self._source.close()
        except Exception:
            pass
        for slot in slots:
            with slot.cv:
                slot.eof = True
                slot.cv.notify_all()

    def _broadcast(self, payload: bytes) -> None:
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            with slot.cv:
                if slot.closed or slot.eof or slot.overflow:
                    continue
                if slot.buffered + len(payload) > slot.max_bytes:
                    # Explicit overflow: do not enqueue, do not block the pump,
                    # do not pretend the reader is still caught up.
                    slot.overflow = True
                    slot.cv.notify_all()
                    continue
                slot.chunks.append(payload)
                slot.buffered += len(payload)
                slot.cv.notify()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._source.read(self._chunk_size)
                if not chunk:
                    break
                self._broadcast(chunk)
        finally:
            self.close()


def add_dictate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration", type=float, default=4.0, help="Recording duration in seconds")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--input-device", default="default", help="PulseAudio input device")
    parser.add_argument("--record-format", choices=["ogg", "wav"], default="ogg")
    parser.add_argument(
        "--record-backend",
        choices=["auto", "ffmpeg-pulse", "arecord"],
        default="auto",
        help="Recorder backend: auto picks ffmpeg(pulse) then arecord",
    )
    parser.add_argument(
        "--commit-backend",
        choices=["none", "auto", "auto-fallback", "fcitx", "wtype", "xdotool", "xdotool-clipboard", "stdout"],
        default="auto",
    )
    parser.add_argument(
        "--auto-hard-enter",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Send a real Enter key event after committing text",
    )
    parser.add_argument(
        "--enable-streaming-commit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Stream model output into the target app incrementally when supported",
    )

    parser.add_argument(
        "--asr-provider",
        choices=["qwen-asr", "http-cloud", "confucius-asr"],
        default="qwen-asr",
        help="ASR provider backend",
    )
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-0.6B")
    parser.add_argument("--qwen-model", default="", help="Qwen3-ASR model path or name; overrides --model for qwen-asr provider (default: Qwen3-ASR-0.6B)")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--hub", default="ms", choices=["ms", "hf"])
    parser.add_argument("--remote-code", default="")
    parser.add_argument(
        "--enable-vad",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable VAD in utterance ASR provider to suppress silence hallucination",
    )
    parser.add_argument("--hotword", action="append", default=[])
    parser.add_argument(
        "--hotword-replacement",
        action="append",
        default=[],
        help="Explicit lexicon replacement SRC→DST (repeatable). Also accepted in --asr-context as '错词 → 正词'.",
    )
    parser.add_argument(
        "--qwen-language",
        default="Chinese",
        help=(
            "Language hint for Qwen3-ASR (e.g. Chinese, English, auto); also passed to "
            "confucius-asr as the stream language header. 'auto' enables automatic detection."
        ),
    )
    parser.add_argument(
        "--qwen-max-new-tokens",
        type=int,
        default=8192,
        help="Max tokens for Qwen3-ASR generation. Higher = handles longer utterances.",
    )
    parser.add_argument(
        "--asr-context-preset",
        default="",
        help="ASR context preset name. Will load presets/asr-{name}.md",
    )
    parser.add_argument(
        "--asr-context",
        default="",
        help="Custom ASR context/hints text appended after preset",
    )
    parser.add_argument(
        "--asr-endpoint",
        default="http://127.0.0.1:8000/v1/audio/transcriptions",
        help="HTTP endpoint for http-cloud ASR provider (OpenAI/vLLM: /v1/audio/transcriptions)",
    )
    parser.add_argument(
        "--asr-api-key",
        default="",
        help=(
            "API key for the ASR provider: http-cloud sends it as Bearer token, "
            "confucius-asr reuses it as the protocol secret_key. Empty means no credential "
            "is sent — whether that is accepted depends on the server; the packaged local "
            "Confucius service requires a token."
        ),
    )
    parser.add_argument(
        "--asr-timeout-s",
        type=float,
        default=30.0,
        help="Timeout in seconds for http-cloud ASR requests",
    )
    parser.add_argument(
        "--asr-realtime-endpoint",
        default="",
        help=(
            "Realtime ASR endpoint. http-cloud: HTTP base URL (example: http://192.168.5.111:40002); "
            "confucius-asr: WebSocket URL (packaged local service: ws://127.0.0.1:8321/asr_stream_api_v1)"
        ),
    )
    add_remote_paste_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Linux dictate once: record mic, ASR, commit text.")
    add_dictate_args(parser)
    return parser


def build_ffmpeg_record_cmd(
    *,
    ffmpeg_bin: str,
    output_path: Path,
    duration_s: float | None,
    sample_rate: int,
    channels: int,
    input_device: str,
    record_format: str,
    enable_monitor: bool = False,
) -> list[str]:
    base = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-y",
        "-f",
        "pulse",
        "-i",
        input_device,
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
    ]
    if duration_s is not None:
        base.extend(["-t", f"{duration_s:.3f}"])
    if enable_monitor:
        base.extend(
            [
                "-filter_complex",
                "[0:a]aformat=sample_fmts=flt:sample_rates=16000:channel_layouts=mono,asplit=2[record][monitor]",
                "-map",
                "[record]",
            ]
        )
    monitor_output = [
        "-map",
        "[monitor]",
        "-flush_packets",
        "1",
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "pipe:1",
    ] if enable_monitor else []
    if record_format == "ogg":
        return base + ["-c:a", "libopus", "-b:a", "24k", str(output_path), *monitor_output]
    if record_format == "wav":
        return base + ["-c:a", "pcm_s16le", str(output_path), *monitor_output]
    raise ValueError(f"unsupported format: {record_format}")


def build_arecord_cmd(
    *,
    output_path: Path,
    duration_s: float | None,
    sample_rate: int,
    channels: int,
    input_device: str = "default",
) -> list[str]:
    cmd = [
        "arecord",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        str(channels),
    ]
    if input_device and input_device != "default":
        cmd.extend(["-D", input_device])
    if duration_s is not None:
        seconds = max(1, math.ceil(duration_s))
        cmd.extend(["-d", str(seconds)])
    cmd.append(str(output_path))
    return cmd


def _ffmpeg_supports_pulse(ffmpeg_bin: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-devices"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    output = f"{proc.stdout}\n{proc.stderr}"
    return " pulse" in output


def choose_record_backend(requested_backend: str, ffmpeg_bin: str | None) -> str:
    if requested_backend == "ffmpeg-pulse":
        if ffmpeg_bin is None:
            raise RuntimeError("ffmpeg unavailable for ffmpeg-pulse backend")
        if not _ffmpeg_supports_pulse(ffmpeg_bin):
            raise RuntimeError("ffmpeg does not support pulse input")
        return "ffmpeg-pulse"
    if requested_backend == "arecord":
        if not which("arecord"):
            raise RuntimeError("arecord not found in PATH")
        return "arecord"
    if ffmpeg_bin is not None and _ffmpeg_supports_pulse(ffmpeg_bin):
        return "ffmpeg-pulse"
    if which("arecord"):
        return "arecord"
    raise RuntimeError("no recorder available: need ffmpeg(pulse) or arecord")


def _resolve_asr_context(args: argparse.Namespace) -> str:
    # ASR context: merge "asr-*" preset + custom context.
    # Do not fall back to refine presets (default/formal/...) to avoid accidental override.
    asr_context_custom = str(getattr(args, "asr_context", "")).strip()
    asr_context_preset = str(getattr(args, "asr_context_preset", "")).strip()
    asr_context_preset_text = ""
    if asr_context_preset:
        from .preset_manager import PresetManager

        preset_mgr = PresetManager()
        preset_name = asr_context_preset if asr_context_preset.startswith("asr-") else f"asr-{asr_context_preset}"
        try:
            asr_context_preset_text = preset_mgr.load_preset(preset_name)
        except FileNotFoundError:
            asr_context_preset_text = ""

    context_parts = [part for part in (asr_context_preset_text, asr_context_custom) if part.strip()]
    return "\n".join(context_parts)


def create_provider(args: argparse.Namespace) -> ASRProvider:
    asr_provider = getattr(args, "asr_provider", "qwen-asr")
    asr_context = _resolve_asr_context(args)

    if asr_provider == "http-cloud":
        # Use HTTP cloud provider
        endpoint = getattr(args, "asr_endpoint", "http://127.0.0.1:8000/v1/audio/transcriptions")
        api_key = str(getattr(args, "asr_api_key", "")).strip() or None
        timeout_s = getattr(args, "asr_timeout_s", 30)
        model_name = str(
            getattr(args, "qwen_model", "")
            or getattr(args, "model", "Qwen/Qwen3-ASR-0.6B")
        ).strip() or "Qwen/Qwen3-ASR-0.6B"
        language = str(getattr(args, "qwen_language", "")).strip()
        return HttpCloudProvider(
            endpoint=endpoint,
            api_key=api_key,
            timeout_s=timeout_s,
            model_name=model_name,
            language=language,
            context=asr_context,
            realtime_endpoint=str(getattr(args, "asr_realtime_endpoint", "")).strip(),
            max_new_tokens=int(getattr(args, "qwen_max_new_tokens", 8192)),
        )

    if asr_provider == "confucius-asr":
        # Confucius4-R2T2 streaming ASR over WebSocket. Endpoint comes from
        # asr_realtime_endpoint; asr_api_key is reused as the protocol secret_key.
        from .providers.confucius_asr import ConfuciusASRProvider

        realtime_endpoint = str(getattr(args, "asr_realtime_endpoint", "")).strip()
        api_key = str(getattr(args, "asr_api_key", "")).strip() or None
        timeout_s = float(getattr(args, "asr_timeout_s", 30) or 30)
        language = str(getattr(args, "qwen_language", "")).strip()
        return ConfuciusASRProvider(
            endpoint=realtime_endpoint,
            api_key=api_key,
            timeout_s=timeout_s,
            language=language,
            context=asr_context,
        )

    # Default to Qwen ASR provider
    # --qwen-model takes priority; fall back to --model; last resort: default
    qwen_model_override = getattr(args, "qwen_model", "")
    if qwen_model_override:
        model = qwen_model_override
    else:
        model = getattr(args, "model", "Qwen/Qwen3-ASR-0.6B")

    raw_lang = cast(str, getattr(args, "qwen_language", "Chinese"))
    qwen_language: str | None = None if raw_lang == "auto" else raw_lang

    device = str(getattr(args, "device", "cuda:0") or "cuda:0")
    if device == "cuda":
        device = "cuda:0"
    provider = QwenASRProvider(
        model_name=model,
        device=device,
        language=qwen_language,
        max_new_tokens=getattr(args, "qwen_max_new_tokens", 1024),
        context=asr_context,
    )
    lazy_load = getattr(provider, "_lazy_load", None)
    if callable(lazy_load):
        import threading

        threading.Thread(target=lazy_load, name="qwen-asr-load", daemon=True).start()
    return provider


def create_committer(args: argparse.Namespace):
    return resolve_committer(args.commit_backend)


def start_record_process(
    *,
    args: argparse.Namespace,
    ffmpeg_bin: str | None,
    recorder_backend: str,
    output_path: Path,
    duration_s: float | None,
    enable_monitor: bool = False,
) -> RecordProcessHandle:
    monitor_enabled = bool(enable_monitor and recorder_backend == "ffmpeg-pulse")
    if recorder_backend == "ffmpeg-pulse":
        assert ffmpeg_bin is not None
        record_cmd = build_ffmpeg_record_cmd(
            ffmpeg_bin=ffmpeg_bin,
            output_path=output_path,
            duration_s=duration_s,
            sample_rate=args.sample_rate,
            channels=args.channels,
            input_device=args.input_device,
            record_format=args.record_format,
            enable_monitor=monitor_enabled,
        )
    else:
        record_cmd = build_arecord_cmd(
            output_path=output_path,
            duration_s=duration_s,
            sample_rate=args.sample_rate,
            channels=args.channels,
            input_device=str(getattr(args, "input_device", "default")),
        )
    stderr_sink = _record_stderr_sink()
    if stderr_sink is not None:
        try:
            header = f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {recorder_backend} {' '.join(record_cmd)} ===\n"
            stderr_sink.write(header.encode("utf-8", errors="replace"))
        except OSError:
            stderr_sink = None
    if monitor_enabled and which("stdbuf"):
        record_cmd = ["stdbuf", "-o0", *record_cmd]
    proc = subprocess.Popen(
        record_cmd,
        stdout=subprocess.PIPE if monitor_enabled else None,
        # 以前这里在 monitor 模式下是 DEVNULL，录音失败（设备忙/被切走）完全无迹可查
        stderr=stderr_sink if stderr_sink is not None else (subprocess.DEVNULL if monitor_enabled else None),
        bufsize=0 if monitor_enabled else -1,
    )
    _ACTIVE_PROCESSES.append(proc)
    backlog_bytes = int(MONITOR_BACKLOG_S * int(args.sample_rate) * max(1, int(args.channels)) * 4)
    monitor_hub = (
        _MonitorFanout(cast(BinaryIO, proc.stdout), max_backlog_bytes=backlog_bytes)
        if monitor_enabled and proc.stdout is not None
        else None
    )
    return RecordProcessHandle(
        process=proc,
        monitor_stream=cast(BinaryIO | None, monitor_hub.open_reader()) if monitor_hub is not None else None,
        monitor_sample_rate=int(args.sample_rate),
        monitor_channels=int(args.channels),
        monitor_hub=monitor_hub,
    )


def _unwrap_record_process_handle(
    process: RecordProcessHandle | subprocess.Popen[Any],
) -> tuple[subprocess.Popen[Any], BinaryIO | None, Any | None]:
    if isinstance(process, RecordProcessHandle):
        return process.process, process.monitor_stream, process.monitor_hub
    return process, None, None


def open_monitor_stream_reader(process: RecordProcessHandle | subprocess.Popen[Any]) -> BinaryIO | None:
    if isinstance(process, RecordProcessHandle):
        monitor_hub = getattr(process, "monitor_hub", None)
        if monitor_hub is not None:
            return cast(BinaryIO, monitor_hub.open_reader())
        return process.monitor_stream
    return None


def stop_record_process(
    process: RecordProcessHandle | subprocess.Popen[Any],
    *,
    recorder_backend: str,
    timeout_s: float = 2.0,
) -> None:
    proc, monitor_stream, monitor_hub = _unwrap_record_process_handle(process)
    if proc.poll() is not None:
        if monitor_stream is not None:
            try:
                monitor_stream.close()
            except Exception:
                pass
        if monitor_hub is not None:
            try:
                monitor_hub.close()
            except Exception:
                pass
        # 进程已退出，从注册表移除
        if proc in _ACTIVE_PROCESSES:
            _ACTIVE_PROCESSES.remove(proc)
        return

    # 发送信号
    if recorder_backend == "ffmpeg-pulse":
        proc.send_signal(signal.SIGINT)
    else:
        proc.send_signal(signal.SIGTERM)

    # 使用 poll 循环代替 wait，提升响应速度
    poll_interval_s = 0.1
    elapsed = 0.0

    while elapsed < timeout_s:
        if proc.poll() is not None:
            if monitor_stream is not None:
                try:
                    monitor_stream.close()
                except Exception:
                    pass
            if monitor_hub is not None:
                try:
                    monitor_hub.close()
                except Exception:
                    pass
            # 进程已退出，从注册表移除
            if proc in _ACTIVE_PROCESSES:
                _ACTIVE_PROCESSES.remove(proc)
            return
        time.sleep(poll_interval_s)
        elapsed += poll_interval_s

    # 超时后强制终止
    try:
        proc.kill()
        proc.wait(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        if monitor_stream is not None:
            try:
                monitor_stream.close()
            except Exception:
                pass
        if monitor_hub is not None:
            try:
                monitor_hub.close()
            except Exception:
                pass
        # 从注册表移除
        if proc in _ACTIVE_PROCESSES:
            _ACTIVE_PROCESSES.remove(proc)


def transcribe_and_commit(
    *,
    args: argparse.Namespace,
    provider: ASRProvider,
    committer: Any,
    audio_path: Path,
    hotwords: list[str],
    auto_hard_enter: bool = False,
) -> tuple[str, float, str | None, dict[str, object]]:
    t1 = time.perf_counter()
    asr = provider.transcribe_file(audio_path, hotwords=hotwords)
    transcribe_latency_ms = (time.perf_counter() - t1) * 1000
    routing = resolve_remote_paste_routing(args)
    from .hotword_corrector import correct_hotwords, lexicon_from_args

    text = asr.text
    _, replacements = lexicon_from_args(args)
    if text.strip() and (hotwords or replacements) and bool(getattr(args, "enable_hotword_correction", True)):
        try:
            max_edits = max(0, int(getattr(args, "hotword_correction_edits", 1)))
        except Exception:
            max_edits = 1
        text, _changes = correct_hotwords(
            text,
            hotwords,
            max_ascii_edits=max_edits,
            replacements=replacements,
        )

    commit_info = {"backend": committer.backend_name, "committed": False, "detail": "disabled"}
    if text.strip():
        if routing.commit_local:
            try:
                result = committer.commit(text)
                detail = str(result.detail)
                if result.committed and auto_hard_enter:
                    enter_result = send_hard_enter(committer)
                    if enter_result.committed:
                        detail = f"{detail};{enter_result.detail}" if detail else str(enter_result.detail)
                    else:
                        detail = f"{detail};{enter_result.detail}" if detail else str(enter_result.detail)
                commit_info = {"backend": result.backend, "committed": result.committed, "detail": detail}
            except Exception as exc:  # noqa: BLE001
                commit_info = {
                    "backend": committer.backend_name,
                    "committed": False,
                    "detail": str(exc),
                }
        else:
            commit_info = {
                "backend": "remote-paste",
                "committed": False,
                "detail": "routed_to_remote_paste",
            }
    else:
        commit_info = {
            "backend": committer.backend_name,
            "committed": False,
            "detail": "empty_text",
        }

    remote_result = send_remote_paste_from_args(
        args,
        text,
        log=lambda message: logger.info(message),
    )
    if remote_result.get("enabled"):
        commit_info["remote_paste"] = remote_result
    if text.strip() and not routing.commit_local:
        commit_info.update(
            {
                "backend": "remote-paste",
                "committed": bool(remote_result.get("sent", False)),
                "detail": str(remote_result.get("detail", "")).strip() or "remote_paste_failed",
            }
        )
    return text, transcribe_latency_ms, getattr(asr, "detected_language", None), commit_info


def run_dictate_once(
    args: argparse.Namespace,
    *,
    provider: ASRProvider | None = None,
    committer: Any | None = None,
) -> DictateResult:
    ffmpeg_bin = ensure_ffmpeg_available()
    recorder_backend = choose_record_backend(args.record_backend, ffmpeg_bin)

    committer = committer or create_committer(args)
    provider = provider or create_provider(args)

    suffix = ".ogg" if args.record_format == "ogg" else ".wav"
    if recorder_backend == "arecord":
        # arecord writes wav directly.
        suffix = ".wav"
    with TemporaryDirectory(prefix="recordian-dictate-") as temp_dir:
        audio_path = Path(temp_dir) / f"input{suffix}"
        t0 = time.perf_counter()
        record_handle = start_record_process(
            args=args,
            ffmpeg_bin=ffmpeg_bin,
            recorder_backend=recorder_backend,
            output_path=audio_path,
            duration_s=args.duration,
        )
        code = record_handle.process.wait()
        if code != 0:
            raise RuntimeError(f"record command failed with exit code={code}")
        record_latency_ms = (time.perf_counter() - t0) * 1000

        from .hotword_corrector import compose_effective_hotwords

        text, transcribe_latency_ms, detected_language, commit_info = transcribe_and_commit(
            args=args,
            provider=provider,
            committer=committer,
            audio_path=audio_path,
            hotwords=compose_effective_hotwords(args),
            auto_hard_enter=bool(getattr(args, "auto_hard_enter", False)),
        )

        return DictateResult(
            audio_path=str(audio_path),
            record_backend=recorder_backend,
            duration_s=args.duration,
            record_latency_ms=record_latency_ms,
            transcribe_latency_ms=transcribe_latency_ms,
            text=text,
            detected_language=detected_language,
            commit=commit_info,
        )


def main() -> None:
    import sys

    from recordian.error_tracker import get_error_tracker

    def handle_exception(exc_type, exc_value, exc_traceback):
        """Global exception handler."""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(exc_value)

    sys.excepthook = handle_exception

    try:
        parser = build_parser()
        args = parser.parse_args()
        result = run_dictate_once(args)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(e)
        raise


if __name__ == "__main__":
    main()
