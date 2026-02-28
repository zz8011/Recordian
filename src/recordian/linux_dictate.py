from __future__ import annotations

import argparse
import atexit
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from shutil import which
import signal
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Any

from .linux_commit import CommitError, resolve_committer, send_hard_enter
from .providers import QwenASRProvider, HttpCloudProvider, ASRProvider
from .runtime_deps import ensure_ffmpeg_available


# 全局进程注册表
_ACTIVE_PROCESSES: list[subprocess.Popen[Any]] = []


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
        choices=["none", "auto", "wtype", "xdotool", "xdotool-clipboard", "stdout"],
        default="auto",
    )
    parser.add_argument(
        "--auto-hard-enter",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Send a real Enter key event after committing text",
    )

    parser.add_argument(
        "--asr-provider",
        choices=["qwen-asr", "http-cloud"],
        default="qwen-asr",
        help="ASR provider backend",
    )
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--qwen-model", default="", help="Qwen3-ASR model path or name; overrides --model for qwen-asr provider (default: Qwen3-ASR-1.7B)")
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
        "--qwen-language",
        default="Chinese",
        help="Language hint for Qwen3-ASR (e.g. Chinese, English, auto). 'auto' enables automatic detection.",
    )
    parser.add_argument(
        "--qwen-max-new-tokens",
        type=int,
        default=1024,
        help="Max tokens for Qwen3-ASR generation. Higher = handles longer utterances.",
    )
    parser.add_argument(
        "--asr-endpoint",
        default="http://localhost:8000/transcribe",
        help="HTTP endpoint for http-cloud ASR provider",
    )
    parser.add_argument(
        "--asr-timeout-s",
        type=float,
        default=30.0,
        help="Timeout in seconds for http-cloud ASR requests",
    )


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
) -> list[str]:
    base = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
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
    if record_format == "ogg":
        return base + ["-c:a", "libopus", "-b:a", "24k", str(output_path)]
    if record_format == "wav":
        return base + ["-c:a", "pcm_s16le", str(output_path)]
    raise ValueError(f"unsupported format: {record_format}")


def build_arecord_cmd(
    *,
    output_path: Path,
    duration_s: float | None,
    sample_rate: int,
    channels: int,
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


def create_provider(args: argparse.Namespace) -> ASRProvider:
    asr_provider = getattr(args, "asr_provider", "qwen-asr")

    if asr_provider == "http-cloud":
        # Use HTTP cloud provider
        endpoint = getattr(args, "asr_endpoint", "http://localhost:8000/transcribe")
        timeout_s = getattr(args, "asr_timeout_s", 30)
        return HttpCloudProvider(endpoint=endpoint, timeout_s=timeout_s)

    # Default to Qwen ASR provider
    # --qwen-model takes priority; fall back to --model; last resort: default
    qwen_model_override = getattr(args, "qwen_model", "")
    if qwen_model_override:
        model = qwen_model_override
    else:
        model = getattr(args, "model", "Qwen/Qwen3-ASR-1.7B")

    raw_lang = getattr(args, "qwen_language", "Chinese")
    language = None if raw_lang == "auto" else raw_lang

    # 处理 ASR context：支持直接文本或 preset 名称
    asr_context = getattr(args, "asr_context", "")
    asr_context_preset = getattr(args, "asr_context_preset", "")

    # 如果指定了 preset，从 presets 目录加载
    if asr_context_preset:
        from .preset_manager import PresetManager
        preset_mgr = PresetManager()
        try:
            asr_context = preset_mgr.load_preset(f"asr-{asr_context_preset}")
        except FileNotFoundError:
            # 如果 asr-{preset} 不存在，尝试直接使用 preset 名称
            try:
                asr_context = preset_mgr.load_preset(asr_context_preset)
            except FileNotFoundError:
                pass  # 使用原始 asr_context

    return QwenASRProvider(
        model_name=model,
        device=getattr(args, "device", "cuda:0"),
        language=language,
        max_new_tokens=getattr(args, "qwen_max_new_tokens", 1024),
        context=asr_context,
    )


def create_committer(args: argparse.Namespace):
    return resolve_committer(args.commit_backend)


def start_record_process(
    *,
    args: argparse.Namespace,
    ffmpeg_bin: str | None,
    recorder_backend: str,
    output_path: Path,
    duration_s: float | None,
) -> subprocess.Popen[Any]:
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
        )
    else:
        record_cmd = build_arecord_cmd(
            output_path=output_path,
            duration_s=duration_s,
            sample_rate=args.sample_rate,
            channels=args.channels,
        )
    proc = subprocess.Popen(record_cmd)
    _ACTIVE_PROCESSES.append(proc)
    return proc


def stop_record_process(
    process: subprocess.Popen[Any],
    *,
    recorder_backend: str,
    timeout_s: float = 2.0,
) -> None:
    if process.poll() is not None:
        # 进程已退出，从注册表移除
        if process in _ACTIVE_PROCESSES:
            _ACTIVE_PROCESSES.remove(process)
        return

    # 发送信号
    if recorder_backend == "ffmpeg-pulse":
        process.send_signal(signal.SIGINT)
    else:
        process.send_signal(signal.SIGTERM)

    # 使用 poll 循环代替 wait，提升响应速度
    poll_interval_s = 0.1
    elapsed = 0.0

    while elapsed < timeout_s:
        if process.poll() is not None:
            # 进程已退出，从注册表移除
            if process in _ACTIVE_PROCESSES:
                _ACTIVE_PROCESSES.remove(process)
            return
        time.sleep(poll_interval_s)
        elapsed += poll_interval_s

    # 超时后强制终止
    try:
        process.kill()
        process.wait(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        # 从注册表移除
        if process in _ACTIVE_PROCESSES:
            _ACTIVE_PROCESSES.remove(process)


def transcribe_and_commit(
    *,
    provider: QwenASRProvider,
    committer: Any,
    audio_path: Path,
    hotwords: list[str],
    auto_hard_enter: bool = False,
) -> tuple[str, float, dict[str, object]]:
    t1 = time.perf_counter()
    asr = provider.transcribe_file(audio_path, hotwords=hotwords)
    transcribe_latency_ms = (time.perf_counter() - t1) * 1000

    commit_info = {"backend": committer.backend_name, "committed": False, "detail": "disabled"}
    if asr.text.strip():
        try:
            result = committer.commit(asr.text)
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
            "backend": committer.backend_name,
            "committed": False,
            "detail": "empty_text",
        }
    return asr.text, transcribe_latency_ms, commit_info


def run_dictate_once(
    args: argparse.Namespace,
    *,
    provider: QwenASRProvider | None = None,
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
        process = start_record_process(
            args=args,
            ffmpeg_bin=ffmpeg_bin,
            recorder_backend=recorder_backend,
            output_path=audio_path,
            duration_s=args.duration,
        )
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"record command failed with exit code={code}")
        record_latency_ms = (time.perf_counter() - t0) * 1000

        text, transcribe_latency_ms, commit_info = transcribe_and_commit(
            provider=provider,
            committer=committer,
            audio_path=audio_path,
            hotwords=args.hotword,
            auto_hard_enter=bool(getattr(args, "auto_hard_enter", False)),
        )

        return DictateResult(
            audio_path=str(audio_path),
            record_backend=recorder_backend,
            duration_s=args.duration,
            record_latency_ms=record_latency_ms,
            transcribe_latency_ms=transcribe_latency_ms,
            text=text,
            commit=commit_info,
        )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_dictate_once(args)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
