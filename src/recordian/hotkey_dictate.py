from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable

from recordian.config import ConfigManager

from .audio import read_wav_mono_f32
from .linux_commit import CommitError, get_focused_window_id, resolve_committer
from .linux_notify import Notification, resolve_notifier
from .linux_dictate import (
    add_dictate_args,
    choose_record_backend,
    create_committer,
    create_provider,
    run_dictate_once,
    start_record_process,
    stop_record_process,
)
from .runtime_deps import ensure_ffmpeg_available

DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Global hotkey dictation daemon: press hotkey to record, transcribe, and commit."
    )
    parser.add_argument("--hotkey", default="<ctrl_r>", help="PTT trigger hotkey")
    parser.add_argument(
        "--stop-hotkey",
        default="",
        help="Stop hotkey for toggle mode (separate start/stop keys); empty = same key as --toggle-hotkey",
    )
    parser.add_argument(
        "--toggle-hotkey",
        default="",
        help="Toggle start hotkey; when set, PTT and toggle run simultaneously in ptt mode",
    )
    parser.add_argument(
        "--exit-hotkey",
        default="<ctrl>+<alt>+q",
        help="Exit daemon hotkey; empty string disables it",
    )
    parser.add_argument("--cooldown-ms", type=int, default=300, help="Ignore repeated triggers within cooldown")
    parser.add_argument("--trigger-mode", choices=["oneshot", "ptt", "toggle"], default="ptt")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-load-config", action="store_true")
    parser.add_argument("--save-config", action="store_true")
    parser.add_argument("--notify-backend", choices=["none", "auto", "notify-send", "stdout"], default="auto")
    parser.add_argument(
        "--warmup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preload model on startup to reduce first-use latency",
    )
    parser.add_argument(
        "--debug-diagnostics",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Emit detailed runtime diagnostics as log events",
    )
    parser.add_argument(
        "--enable-text-refine",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable text refinement with Qwen3 LLM (remove duplicates, filler words, fix punctuation)",
    )
    parser.add_argument(
        "--refine-model",
        default="Qwen/Qwen3-0.6B",
        help="Text refinement model name or path (default: Qwen3-0.6B for faster speed)",
    )
    parser.add_argument(
        "--refine-device",
        default="cuda",
        help="Device for text refinement model",
    )
    parser.add_argument(
        "--refine-max-tokens",
        type=int,
        default=512,
        help="Max tokens for text refinement generation",
    )
    parser.add_argument(
        "--refine-prompt",
        default="",
        help="Custom prompt template for text refinement (use {text} as placeholder)",
    )
    parser.add_argument(
        "--refine-preset",
        default="default",
        help="Preset name for text refinement (from presets/ directory)",
    )
    parser.add_argument(
        "--refine-provider",
        choices=["local", "cloud"],
        default="local",
        help="Text refinement provider: local (Qwen3-0.6B) or cloud (API)",
    )
    parser.add_argument(
        "--refine-api-base",
        default="https://api.minimaxi.com/anthropic",
        help="API base URL for cloud provider",
    )
    parser.add_argument(
        "--refine-api-key",
        default="",
        help="API key for cloud provider",
    )
    parser.add_argument(
        "--refine-api-model",
        default="claude-3-5-sonnet-20241022",
        help="Model name for cloud provider",
    )
    parser.add_argument(
        "--enable-streaming-refine",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable streaming output for text refinement (real-time display)",
    )
    add_dictate_args(parser)
    return parser


def build_hotkey_handlers(
    *,
    args: argparse.Namespace,
    on_result: Callable[[dict[str, object]], None],
    on_error: Callable[[dict[str, object]], None],
    on_busy: Callable[[dict[str, object]], None],
) -> tuple[Callable[[], None], Callable[[], None], threading.Event]:
    """Create trigger and exit handlers for hotkey events."""
    lock = threading.Lock()
    stop_event = threading.Event()
    cooldown_s = max(0.0, args.cooldown_ms / 1000.0)
    state = {"last_trigger": 0.0}

    def _run_once() -> None:
        now = time.monotonic()
        if now - state["last_trigger"] < cooldown_s:
            return
        state["last_trigger"] = now

        if not lock.acquire(blocking=False):
            on_busy({"event": "busy", "reason": "dictation_in_progress"})
            return

        def _worker() -> None:
            try:
                result = run_dictate_once(args)
                on_result({"event": "result", "result": asdict(result)})
            except Exception as exc:  # noqa: BLE001
                on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                lock.release()

        threading.Thread(target=_worker, daemon=True).start()

    def _exit() -> None:
        stop_event.set()

    return _run_once, _exit, stop_event



def _normalize_final_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    while True:
        half = len(normalized) // 2
        if len(normalized) % 2 == 0 and half > 0 and normalized[:half] == normalized[half:]:
            normalized = normalized[:half]
            continue
        break
    max_tail = min(16, len(normalized) // 2)
    for tail in range(max_tail, 1, -1):
        seg = normalized[-tail:]
        if normalized.endswith(seg + seg):
            normalized = normalized[:-tail]
            break
    return normalized


def _preview_text(text: str, max_len: int = 48) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def _commit_text(committer: Any, text: str) -> dict[str, object]:
    stripped = text.strip()
    if not stripped:
        return {"backend": committer.backend_name, "committed": False, "detail": "empty_text"}
    try:
        result = committer.commit(stripped)
        return {"backend": result.backend, "committed": result.committed, "detail": result.detail}
    except Exception as exc:  # noqa: BLE001
        return {"backend": committer.backend_name, "committed": False, "detail": str(exc)}


def _apply_target_window(committer: Any, state: dict[str, object]) -> None:
    """Set target_window_id on committer if it supports it."""
    wid = state.get("target_window_id")
    if hasattr(committer, "target_window_id"):
        committer.target_window_id = wid if isinstance(wid, int) else None


def build_ptt_hotkey_handlers(
    *,
    args: argparse.Namespace,
    on_result: Callable[[dict[str, object]], None],
    on_error: Callable[[dict[str, object]], None],
    on_busy: Callable[[dict[str, object]], None],
    on_state: Callable[[dict[str, object]], None],
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], None], threading.Event]:
    lock = threading.Lock()
    stop_event = threading.Event()
    cooldown_s = max(0.0, args.cooldown_ms / 1000.0)
    ffmpeg_bin = ensure_ffmpeg_available()
    recorder_backend = choose_record_backend(args.record_backend, ffmpeg_bin)
    committer = resolve_committer(args.commit_backend)
    provider = create_provider(args)

    # Initialize text refiner if enabled
    refiner = None
    if getattr(args, "enable_text_refine", False):
        from .preset_manager import PresetManager

        # 优先级：--refine-prompt > --refine-preset > default preset
        custom_prompt = getattr(args, "refine_prompt", "")
        if not custom_prompt:
            # 使用 preset
            preset_name = getattr(args, "refine_preset", "default")
            preset_mgr = PresetManager()
            try:
                custom_prompt = preset_mgr.load_preset(preset_name)
                on_state({"event": "log", "message": f"使用预设: {preset_name}"})
            except FileNotFoundError as e:
                on_state({"event": "log", "message": f"预设加载失败: {e}"})
                custom_prompt = None

        # 选择 provider：local, cloud, llamacpp
        refine_provider = getattr(args, "refine_provider", "local")

        if refine_provider == "cloud":
            from .providers import CloudLLMRefiner
            api_key = getattr(args, "refine_api_key", "")
            if not api_key:
                raise RuntimeError("使用 cloud provider 需要设置 --refine-api-key")

            refiner = CloudLLMRefiner(
                api_base=getattr(args, "refine_api_base", "https://api.minimaxi.com/anthropic"),
                api_key=api_key,
                model=getattr(args, "refine_api_model", "claude-3-5-sonnet-20241022"),
                max_tokens=getattr(args, "refine_max_tokens", 512),
                temperature=0.1,
                prompt_template=custom_prompt if custom_prompt else None,
                enable_thinking=getattr(args, "enable_thinking", False),
            )
            on_state({"event": "log", "message": f"使用云端 LLM: {refiner.model}"})
        elif refine_provider == "llamacpp":
            from .providers import LlamaCppTextRefiner
            model_path = getattr(args, "refine_model", "")
            if not model_path:
                raise RuntimeError("使用 llamacpp provider 需要设置 --refine-model 为 GGUF 模型路径")

            refiner = LlamaCppTextRefiner(
                model_path=model_path,
                n_gpu_layers=getattr(args, "refine_n_gpu_layers", -1),
                max_new_tokens=getattr(args, "refine_max_tokens", 512),
                temperature=0.1,
                prompt_template=custom_prompt if custom_prompt else None,
                enable_thinking=getattr(args, "enable_thinking", False),
            )
            on_state({"event": "log", "message": f"使用 llama.cpp: {refiner.provider_name}"})
        else:
            from .providers import Qwen3TextRefiner
            refiner = Qwen3TextRefiner(
                model_name=getattr(args, "refine_model", "Qwen/Qwen3-0.6B"),
                device=getattr(args, "refine_device", "cuda"),
                max_new_tokens=getattr(args, "refine_max_tokens", 512),
                prompt_template=custom_prompt if custom_prompt else None,
                enable_thinking=getattr(args, "enable_thinking", False),
            )
            on_state({"event": "log", "message": f"使用本地模型: {refiner.model_name}"})


    if args.warmup:
        on_state({"event": "model_warmup", "status": "starting", "provider": provider.provider_name})
        t0 = time.perf_counter()
        with TemporaryDirectory(prefix="recordian-ptt-warmup-") as temp_dir:
            wav_path = Path(temp_dir) / "warmup.wav"
            # Create a minimal valid WAV file with 0.1s of silence (1600 samples at 16kHz)
            import struct
            sample_rate = 16000
            num_samples = 1600  # 0.1 second
            data_size = num_samples * 2  # 16-bit samples
            wav_data = struct.pack('<4sI4s4sIHHIIHH4sI',
                b'RIFF',
                36 + data_size,
                b'WAVE',
                b'fmt ',
                16,  # fmt chunk size
                1,   # PCM
                1,   # mono
                sample_rate,
                sample_rate * 2,  # byte rate
                2,   # block align
                16,  # bits per sample
                b'data',
                data_size
            ) + b'\x00' * data_size
            wav_path.write_bytes(wav_data)
            provider.transcribe_file(wav_path, hotwords=[])
        on_state({"event": "model_warmup", "status": "ready", "provider": provider.provider_name, "latency_ms": (time.perf_counter() - t0) * 1000})

        # Warmup refiner if enabled
        if refiner:
            on_state({"event": "refiner_warmup", "status": "starting", "provider": refiner.provider_name})
            t0 = time.perf_counter()
            refiner.refine("测试")
            on_state({"event": "refiner_warmup", "status": "ready", "provider": refiner.provider_name, "latency_ms": (time.perf_counter() - t0) * 1000})

    state: dict[str, object] = {
        "last_trigger": 0.0,
        "process": None,
        "temp_dir": None,
        "audio_path": None,
        "record_started_at": None,
        "target_window_id": None,
        "level_stop": None,  # threading.Event to stop audio level sampling
    }

    def _start_recording() -> None:
        now = time.monotonic()
        if now - float(state["last_trigger"]) < cooldown_s:
            return
        state["last_trigger"] = now

        state["target_window_id"] = get_focused_window_id()

        if not lock.acquire(blocking=False):
            on_busy({"event": "busy", "reason": "dictation_in_progress"})
            return

        try:
            temp_dir = TemporaryDirectory(prefix="recordian-ptt-")
            suffix = ".ogg" if args.record_format == "ogg" else ".wav"
            if recorder_backend == "arecord":
                suffix = ".wav"
            audio_path = Path(temp_dir.name) / f"input{suffix}"
            process = start_record_process(
                args=args,
                ffmpeg_bin=ffmpeg_bin,
                recorder_backend=recorder_backend,
                output_path=audio_path,
                duration_s=None,
            )
            state["process"] = process
            state["temp_dir"] = temp_dir
            state["audio_path"] = audio_path
            state["record_started_at"] = time.perf_counter()
            on_state({"event": "recording_started", "record_backend": recorder_backend, "audio_path": str(audio_path)})

            # Start audio level sampling thread
            level_stop = threading.Event()
            state["level_stop"] = level_stop

            def _level_worker(stop: threading.Event = level_stop) -> None:
                try:
                    import sounddevice as sd
                    import numpy as np

                    def _cb(indata: Any, frames: int, t: Any, status: Any) -> None:
                        if stop.is_set():
                            raise sd.CallbackStop()
                        rms = float(np.sqrt(np.mean(indata ** 2)))
                        # Lower sensitivity and higher noise gate
                        on_state({"event": "audio_level", "level": min(1.0, max(0.0, rms * 1.8 - 0.02))})

                    with sd.InputStream(samplerate=16000, channels=1, blocksize=1024, callback=_cb):
                        stop.wait()
                except ImportError:
                    # sounddevice not available, skip audio level monitoring
                    if args.debug_diagnostics:
                        on_state({"event": "log", "message": "diag audio_level_monitoring_disabled: sounddevice not installed"})
                except Exception as exc:  # noqa: BLE001
                    # Other errors (e.g., no audio device), skip silently
                    if args.debug_diagnostics:
                        on_state({"event": "log", "message": f"diag audio_level_monitoring_failed: {exc}"})

            threading.Thread(target=_level_worker, daemon=True).start()
        except Exception:  # noqa: BLE001
            lock.release()
            raise

    def _stop_recording() -> None:
        process = state.get("process")
        started = state.get("record_started_at")
        audio_path = state.get("audio_path")
        temp_dir = state.get("temp_dir")
        if process is None or audio_path is None or temp_dir is None or started is None:
            return

        # Stop audio level sampling
        level_stop = state.get("level_stop")
        if isinstance(level_stop, threading.Event):
            level_stop.set()
        state["level_stop"] = None

        state["process"] = None
        state["audio_path"] = None
        state["temp_dir"] = None
        state["record_started_at"] = None

        try:
            stop_record_process(process, recorder_backend=recorder_backend)
        except Exception as exc:  # noqa: BLE001
            temp_dir.cleanup()
            lock.release()
            on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            return

        record_latency_ms = (time.perf_counter() - float(started)) * 1000
        audio_path = Path(audio_path)
        on_state({"event": "processing_started", "record_backend": recorder_backend, "audio_path": str(audio_path), "record_latency_ms": record_latency_ms})

        def _worker() -> None:
            try:
                import numpy as np
                samples = read_wav_mono_f32(audio_path)
                rms = float(np.sqrt(np.mean(samples ** 2)))
                if rms < 0.003:
                    on_state({"event": "log", "message": f"静音跳过 ASR (rms={rms:.4f})"})
                    return

                t0 = time.perf_counter()
                asr = provider.transcribe_file(audio_path, hotwords=args.hotword)
                transcribe_latency_ms = (time.perf_counter() - t0) * 1000
                text = _normalize_final_text(asr.text)

                # Apply text refinement if enabled
                refine_latency_ms = 0.0
                if refiner and text.strip():
                    # 检查配置是否变化，动态更新 preset（热切换）
                    if hasattr(refiner, 'update_preset'):
                        try:
                            # 重新读取配置文件
                            import json
                            config_path = getattr(args, 'config_path', None)
                            if config_path and Path(config_path).exists():
                                with open(config_path, 'r', encoding='utf-8') as f:
                                    current_config = json.load(f)
                                new_preset = current_config.get('refine_preset', 'default')
                                old_preset = getattr(args, 'refine_preset', 'default')

                                # 如果 preset 变化，更新 refiner
                                if new_preset != old_preset:
                                    refiner.update_preset(new_preset)
                                    args.refine_preset = new_preset  # 更新 args 中的值
                                    on_state({"event": "log", "message": f"已切换到 {new_preset} preset"})
                        except Exception as e:
                            on_state({"event": "log", "message": f"preset 热切换失败: {e}"})

                    t1 = time.perf_counter()

                    # 调试：输出 ASR 原始文本
                    on_state({"event": "log", "message": f"ASR 原始输出: {text}"})

                    # 使用流式输出或非流式输出
                    if getattr(args, "enable_streaming_refine", False):
                        # 流式输出：逐字显示
                        refined_text = ""
                        for chunk in refiner.refine_stream(text):
                            refined_text += chunk
                            # 发送流式更新事件
                            on_state({
                                "event": "refine_stream_chunk",
                                "chunk": chunk,
                                "accumulated": refined_text,
                            })
                    else:
                        # 非流式输出：一次性返回
                        refined_text = refiner.refine(text)

                    # 调试：输出精炼后文本
                    on_state({"event": "log", "message": f"精炼后输出: {refined_text}"})

                    refine_latency_ms = (time.perf_counter() - t1) * 1000
                    if refined_text.strip():
                        text = refined_text
                    if args.debug_diagnostics:
                        on_state({"event": "log", "message": (
                            f"text_refine original_len={len(asr.text)}"
                            f" refined_len={len(text)}"
                            f" latency_ms={refine_latency_ms:.1f}"
                            f" streaming={getattr(args, 'enable_streaming_refine', False)}"
                        )})

                _apply_target_window(committer, state)
                commit_info = _commit_text(committer, text)
                if args.debug_diagnostics:
                    on_state({"event": "log", "message": (
                        "diag finalize source=oneshot"
                        f" text_len={len(text)}"
                        f" committed={bool(commit_info.get('committed', False))}"
                        f" commit_backend={commit_info.get('backend', '')}"
                        f" commit_detail={commit_info.get('detail', '')}"
                        f" text={_preview_text(text)}"
                    )})
                on_result({"event": "result", "result": {
                    "audio_path": str(audio_path),
                    "record_backend": recorder_backend,
                    "duration_s": record_latency_ms / 1000.0,
                    "record_latency_ms": record_latency_ms,
                    "transcribe_latency_ms": transcribe_latency_ms,
                    "refine_latency_ms": refine_latency_ms,
                    "text": text,
                    "commit": commit_info,
                }})
            except Exception as exc:  # noqa: BLE001
                on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                temp_dir.cleanup()
                lock.release()

        threading.Thread(target=_worker, daemon=True).start()

    def _exit() -> None:
        _stop_recording()
        stop_event.set()

    return _start_recording, _stop_recording, _exit, stop_event


def _parse_args_with_config(parser: argparse.ArgumentParser) -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    pre.add_argument("--no-load-config", action="store_true")
    pre_args, _ = pre.parse_known_args()

    config_path = Path(pre_args.config_path).expanduser()
    if not pre_args.no_load_config and config_path.exists():
        payload = ConfigManager.load(config_path)
        if isinstance(payload, dict):
            allowed = {
                action.dest
                for action in parser._actions
                if action.dest not in {"help", "save_config", "no_load_config"}
            }
            defaults = {k: v for k, v in payload.items() if k in allowed}
            if defaults:
                parser.set_defaults(**defaults)

    args = parser.parse_args()
    args.config_path = str(Path(args.config_path).expanduser())
    return args


def _save_runtime_config(args: argparse.Namespace) -> None:
    payload = {
        "hotkey": args.hotkey,
        "stop_hotkey": getattr(args, "stop_hotkey", ""),
        "toggle_hotkey": getattr(args, "toggle_hotkey", ""),
        "exit_hotkey": args.exit_hotkey,
        "cooldown_ms": args.cooldown_ms,
        "trigger_mode": args.trigger_mode,
        "notify_backend": args.notify_backend,
        "duration": args.duration,
        "sample_rate": args.sample_rate,
        "channels": args.channels,
        "input_device": args.input_device,
        "record_format": args.record_format,
        "record_backend": args.record_backend,
        "commit_backend": args.commit_backend,
        "model": args.model,
        "device": args.device,
        "hub": args.hub,
        "warmup": args.warmup,
        "debug_diagnostics": args.debug_diagnostics,
        "remote_code": args.remote_code,
        "hotword": list(args.hotword),
        "asr_provider": getattr(args, "asr_provider", "qwen-asr"),
        "qwen_model": getattr(args, "qwen_model", ""),
        "qwen_language": getattr(args, "qwen_language", "Chinese"),
        "qwen_max_new_tokens": getattr(args, "qwen_max_new_tokens", 1024),
        "asr_context": getattr(args, "asr_context", ""),
        "asr_context_preset": getattr(args, "asr_context_preset", ""),
        "enable_text_refine": getattr(args, "enable_text_refine", False),
        "refine_provider": getattr(args, "refine_provider", "local"),
        "refine_model": getattr(args, "refine_model", "Qwen/Qwen3-0.6B"),
        "refine_device": getattr(args, "refine_device", "cuda"),
        "refine_max_tokens": getattr(args, "refine_max_tokens", 512),
        "refine_prompt": getattr(args, "refine_prompt", ""),
        "refine_preset": getattr(args, "refine_preset", "default"),
        "refine_api_base": getattr(args, "refine_api_base", "https://api.minimaxi.com/anthropic"),
        "refine_api_key": getattr(args, "refine_api_key", ""),
        "refine_api_model": getattr(args, "refine_api_model", "claude-3-5-sonnet-20241022"),
        "enable_streaming_refine": getattr(args, "enable_streaming_refine", False),
    }
    path = Path(args.config_path)
    ConfigManager.save(path, payload)


def parse_hotkey_spec(spec: str) -> set[str]:
    tokens = [part.strip().lower() for part in spec.split("+") if part.strip()]
    normalized: set[str] = set()
    alias = {
        "control": "ctrl",
        "ctl": "ctrl",
        "rctrl": "ctrl_r",
        "rightctrl": "ctrl_r",
        "ctrl-right": "ctrl_r",
        "lctrl": "ctrl_l",
        "leftctrl": "ctrl_l",
        "ctrl-left": "ctrl_l",
        "option": "alt",
        "super": "cmd",
        "win": "cmd",
        "windows": "cmd",
        "return": "enter",
        "application": "menu",
        "app": "menu",
        "0xff67": "menu",
    }
    for token in tokens:
        if token.startswith("<") and token.endswith(">") and len(token) > 2:
            token = token[1:-1]
        if token.startswith(("vk:", "keycode:", "kc:")):
            _, value = token.split(":", 1)
            if not value:
                continue
            if value.isdigit():
                normalized.add(f"vk:{int(value)}")
                continue
        if token.isdigit():
            normalized.add(f"vk:{int(token)}")
            continue
        normalized.add(alias.get(token, token))
    return normalized


def _expand_key_name(name: str) -> set[str]:
    token = name.strip().lower()
    if not token:
        return set()
    alias_map = {
        "app": "menu",
        "application": "menu",
        "menu": "menu",
    }
    token = alias_map.get(token, token)
    expanded = {token}
    if token in {"ctrl_l", "ctrl_r"}:
        expanded.add("ctrl")
    elif token in {"alt_l", "alt_r", "alt_gr"}:
        expanded.add("alt")
    elif token in {"shift_l", "shift_r"}:
        expanded.add("shift")
    elif token in {"cmd_l", "cmd_r"}:
        expanded.add("cmd")
    return expanded


def _key_to_names(key: object, keyboard_module: Any) -> set[str]:
    if isinstance(key, keyboard_module.KeyCode):
        names: set[str] = set()
        if key.char:
            names.add(key.char.lower())
        vk = getattr(key, "vk", None)
        if vk is not None:
            names.add(f"vk:{int(vk)}")
        return names

    if isinstance(key, keyboard_module.Key):
        name = (key.name or "").lower()
        return _expand_key_name(name)
    return set()


def main() -> None:
    parser = build_parser()
    args = _parse_args_with_config(parser)
    if args.save_config:
        _save_runtime_config(args)

    try:
        from pynput import keyboard
    except ModuleNotFoundError as exc:
        raise RuntimeError("pynput not installed. Run: pip install -e '.[hotkey]'") from exc

    def _print_json(payload: dict[str, object]) -> None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    notifier = resolve_notifier(args.notify_backend)

    def _notify(payload: dict[str, object]) -> None:
        event = str(payload.get("event", ""))
        if event == "ready":
            notifier.notify(
                Notification(
                    title="Recordian 已就绪",
                    body=f"触发: {args.hotkey} 退出: {args.exit_hotkey or '禁用'}",
                    urgency="low",
                )
            )
            return
        if event == "recording_started":
            notifier.notify(Notification(title="Recordian", body="开始录音", urgency="low"))
            return
        if event == "busy":
            notifier.notify(Notification(title="Recordian", body="仍在处理上一条语音", urgency="low"))
            return
        if event == "error":
            detail = str(payload.get("error", "unknown_error"))
            notifier.notify(Notification(title="Recordian 错误", body=detail, urgency="critical"))
            return
        if event == "result":
            result = payload.get("result")
            text = ""
            if isinstance(result, dict):
                text = str(result.get("text", "")).strip()
            body = _truncate_text(text, max_len=40) if text else "识别为空"
            notifier.notify(Notification(title="Recordian 识别完成", body=body, urgency="normal"))
            return
        if event == "stopped":
            notifier.notify(Notification(title="Recordian 已退出", body="热键守护进程已停止", urgency="low"))

    def _emit(payload: dict[str, object]) -> None:
        _print_json(payload)
        try:
            _notify(payload)
        except Exception:
            # Notification failure should not break dictation flow.
            pass

    trigger_keys = parse_hotkey_spec(args.hotkey)
    stop_keys = parse_hotkey_spec(args.stop_hotkey) if getattr(args, "stop_hotkey", "").strip() else set()
    toggle_keys = parse_hotkey_spec(args.toggle_hotkey) if getattr(args, "toggle_hotkey", "").strip() else set()
    exit_keys = parse_hotkey_spec(args.exit_hotkey) if args.exit_hotkey.strip() else set()
    if not trigger_keys:
        raise RuntimeError("empty hotkey is not allowed")

    if args.trigger_mode in {"ptt", "toggle"}:
        start_recording, stop_recording, exit_daemon, stop_event = build_ptt_hotkey_handlers(
            args=args,
            on_result=_emit,
            on_error=_emit,
            on_busy=_emit,
            on_state=_emit,
        )
        trigger_pressed = {"active": False}

        pressed: set[str] = set()

        if args.trigger_mode == "ptt":
            toggle_recording = {"active": False}
            toggle_pressed = {"active": False}
            stop_pressed = {"active": False}

            def _on_press(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.update(key_names)
                if exit_keys and exit_keys.issubset(pressed):
                    exit_daemon()
                    return False

                # Toggle stop key — only intercept when toggle is active
                if stop_keys and stop_keys.issubset(pressed) and not stop_pressed["active"] and toggle_recording["active"]:
                    stop_pressed["active"] = True
                    toggle_recording["active"] = False
                    stop_recording()
                    return True

                # Toggle start key (only when not a subset of PTT key to avoid double-trigger)
                if toggle_keys and toggle_keys.issubset(pressed) and not toggle_pressed["active"]:
                    toggle_pressed["active"] = True
                    if not toggle_recording["active"]:
                        toggle_recording["active"] = True
                        try:
                            start_recording()
                        except Exception as exc:  # noqa: BLE001
                            toggle_recording["active"] = False
                            _emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
                    return True

                # PTT: only when toggle is not active
                if trigger_keys.issubset(pressed) and not trigger_pressed["active"] and not toggle_recording["active"]:
                    trigger_pressed["active"] = True
                    try:
                        start_recording()
                    except Exception as exc:  # noqa: BLE001
                        _emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
                return True

            def _on_release(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.difference_update(key_names)
                if toggle_pressed["active"] and not (toggle_keys and toggle_keys.issubset(pressed)):
                    toggle_pressed["active"] = False
                if stop_pressed["active"] and not (stop_keys and stop_keys.issubset(pressed)):
                    stop_pressed["active"] = False
                # PTT release: stop only if PTT was active (not toggle)
                if trigger_pressed["active"] and not trigger_keys.issubset(pressed):
                    trigger_pressed["active"] = False
                    if not toggle_recording["active"]:
                        stop_recording()
                if stop_event.is_set():
                    return False
                return True
        else:
            # toggle: start and stop can be different keys
            recording = {"active": False}
            stop_trigger_pressed = {"active": False}

            def _on_press(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.update(key_names)
                if exit_keys and exit_keys.issubset(pressed):
                    exit_daemon()
                    return False
                # Dedicated stop key
                if stop_keys and stop_keys.issubset(pressed) and not stop_trigger_pressed["active"]:
                    stop_trigger_pressed["active"] = True
                    if recording["active"]:
                        recording["active"] = False
                        stop_recording()
                    return True
                # Start key
                if trigger_keys.issubset(pressed) and not trigger_pressed["active"]:
                    trigger_pressed["active"] = True
                    if not recording["active"]:
                        recording["active"] = True
                        try:
                            start_recording()
                        except Exception as exc:  # noqa: BLE001
                            recording["active"] = False
                            _emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
                    elif not stop_keys:
                        # No dedicated stop key: same key toggles off
                        recording["active"] = False
                        stop_recording()
                return True

            def _on_release(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.difference_update(key_names)
                if trigger_pressed["active"] and not trigger_keys.issubset(pressed):
                    trigger_pressed["active"] = False
                if stop_trigger_pressed["active"] and not (stop_keys and stop_keys.issubset(pressed)):
                    stop_trigger_pressed["active"] = False
                if stop_event.is_set():
                    return False
                return True
    else:
        run_once, exit_daemon, stop_event = build_hotkey_handlers(
            args=args,
            on_result=_emit,
            on_error=_emit,
            on_busy=_emit,
        )
        trigger_pressed = {"active": False}

        pressed: set[str] = set()

        def _on_press(key: object):
            key_names = _key_to_names(key, keyboard)
            if not key_names:
                return True
            pressed.update(key_names)
            if exit_keys and exit_keys.issubset(pressed):
                exit_daemon()
                return False
            if trigger_keys.issubset(pressed) and not trigger_pressed["active"]:
                trigger_pressed["active"] = True
                run_once()
            return True

        def _on_release(key: object):
            key_names = _key_to_names(key, keyboard)
            if not key_names:
                return True
            pressed.difference_update(key_names)
            if trigger_pressed["active"] and not trigger_keys.issubset(pressed):
                trigger_pressed["active"] = False
            if stop_event.is_set():
                return False
            return True

    _emit(
        {
            "event": "ready",
            "hotkey": args.hotkey,
            "exit_hotkey": args.exit_hotkey,
            "cooldown_ms": args.cooldown_ms,
            "trigger_mode": args.trigger_mode,
            "config_path": args.config_path,
            "notify_backend": notifier.backend_name,
        }
    )

    with keyboard.Listener(on_press=_on_press, on_release=_on_release) as listener:
        while not stop_event.is_set():
            time.sleep(0.1)
        listener.stop()

    _emit({"event": "stopped"})


def _truncate_text(text: str, *, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


if __name__ == "__main__":
    main()


def _merge_stream_text(prev: str, current: str) -> str:
    """合并流式 ASR 文本：若 current 以 prev 开头则直接返回 current，否则拼接。"""
    if not prev:
        return current
    if current.startswith(prev):
        return current
    if prev.endswith(current):
        return prev
    return prev + current


def _adaptive_vad_threshold(base: float, noise_level: float) -> float:
    """根据环境噪声动态调整 VAD 阈值，下限为 base * 0.4，上限为 base。"""
    min_thresh = base * 0.4
    if noise_level <= 0.0:
        return min_thresh
    if noise_level >= base:
        return base
    ratio = noise_level / base
    return min_thresh + (base - min_thresh) * ratio


def _pcm16le_to_f32(data: bytes, *, channels: int = 1) -> list:
    """将 PCM 16-bit LE 字节转换为 float32 单声道列表（多声道取平均）。"""
    import struct
    n_samples = len(data) // 2
    samples = struct.unpack(f"<{n_samples}h", data)
    frames = n_samples // channels
    result = []
    for i in range(frames):
        frame = samples[i * channels : (i + 1) * channels]
        avg = sum(frame) / channels / 32768.0
        result.append(avg)
    return result
