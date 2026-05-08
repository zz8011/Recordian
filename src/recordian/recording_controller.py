"""Recording control and hotkey handler factories for Recordian.

Provides :func:`build_hotkey_handlers` (oneshot mode) and
:func:`build_ptt_hotkey_handlers` (PTT / toggle mode), along with
helper functions for audio cues and text commit.
"""
from __future__ import annotations

import argparse
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from .audio_feedback import play_sound
from .auto_lexicon import AutoLexicon
from .linux_commit import (
    get_focused_window_id,
    paste_to_enter_delay_seconds,
    resolve_committer,
    send_hard_enter,
)
from .linux_dictate import (
    RecordProcessHandle,
    choose_record_backend,
    create_provider,
    run_dictate_once,
    start_record_process,
    stop_record_process,
)
from .postprocess_pipeline import (
    PostprocessPipelineContext,
    _apply_target_window,
    _extract_refine_postprocess_rule,
    _resolve_auto_hard_enter,
    run_postprocess_pipeline,
)
from .realtime_asr import (
    _RealtimeASRWorkerHandle,
    _start_realtime_asr_worker,
)
from .remote_paste.client import resolve_remote_paste_routing
from .runtime_deps import ensure_ffmpeg_available
from .state_manager import RecordingState
from .text_cleanup import _normalize_final_text
from .wake_session_monitor import (
    WakeSessionMonitorContext,
    start_wake_session_monitor,
)


def build_hotkey_handlers(
    *,
    args: argparse.Namespace,
    on_result: Callable[[dict[str, object]], None],
    on_error: Callable[[dict[str, object]], None],
    on_busy: Callable[[dict[str, object]], None],
) -> tuple[Callable[[], None], Callable[[], None], threading.Event]:
    """Create trigger and exit handlers for hotkey events."""
    state_lock = threading.Lock()
    run_lock = threading.Lock()
    stop_event = threading.Event()
    cooldown_s = max(0.0, args.cooldown_ms / 1000.0)
    state = {"last_trigger": 0.0}

    def _run_once() -> None:
        now = time.monotonic()
        # 使用独立状态锁保护节流读写，避免与运行锁互相干扰
        with state_lock:
            if now - state["last_trigger"] < cooldown_s:
                return
            state["last_trigger"] = now

        if not run_lock.acquire(blocking=False):
            on_busy({"event": "busy", "reason": "dictation_in_progress"})
            return

        def _worker() -> None:
            try:
                _play_global_cue(args, "on")
                result = run_dictate_once(args)
                on_result({"event": "result", "result": asdict(result)})
            except Exception as exc:  # noqa: BLE001
                on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                _play_global_cue(args, "off")
                run_lock.release()

        threading.Thread(target=_worker, daemon=True).start()

    def _exit() -> None:
        stop_event.set()

    return _run_once, _exit, stop_event


def _play_global_cue(args: argparse.Namespace, cue: str) -> None:
    custom_path = getattr(args, "sound_on_path", "") if cue == "on" else getattr(args, "sound_off_path", "")
    # Backward compatibility: older config only had wake_beep_path.
    legacy = getattr(args, "wake_beep_path", "")
    play_sound(cue=cue, custom_path=custom_path, legacy_beep_path=legacy)


def _commit_text(committer: Any, text: str, *, auto_hard_enter: bool = False) -> dict[str, object]:
    stripped = text.strip()
    if not stripped:
        return {"backend": committer.backend_name, "committed": False, "detail": "empty_text"}
    try:
        result = committer.commit(stripped)
        detail = str(result.detail)
        if result.committed and auto_hard_enter:
            enter_delay_s = paste_to_enter_delay_seconds(result)
            if enter_delay_s > 0.0:
                time.sleep(enter_delay_s)
            enter_result = send_hard_enter(committer)
            enter_detail = str(enter_result.detail)
            detail = f"{detail};{enter_detail}" if detail else enter_detail
        return {"backend": result.backend, "committed": result.committed, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"backend": committer.backend_name, "committed": False, "detail": str(exc)}


def build_ptt_hotkey_handlers(
    *,
    args: argparse.Namespace,
    on_result: Callable[[dict[str, object]], None],
    on_error: Callable[[dict[str, object]], None],
    on_busy: Callable[[dict[str, object]], None],
    on_state: Callable[[dict[str, object]], None],
) -> tuple[Callable[..., bool], Callable[[], bool], Callable[[], None], threading.Event]:
    lock = threading.Lock()
    stop_event = threading.Event()
    cooldown_s = max(0.0, args.cooldown_ms / 1000.0)
    ffmpeg_bin = ensure_ffmpeg_available()
    recorder_backend = choose_record_backend(args.record_backend, ffmpeg_bin)
    committer = resolve_committer(args.commit_backend)
    provider = create_provider(args)
    auto_lexicon: AutoLexicon | None = None
    if bool(getattr(args, "enable_auto_lexicon", True)):
        try:
            auto_lexicon = AutoLexicon(
                db_path=Path(getattr(args, "auto_lexicon_db", "~/.config/recordian/auto_lexicon.db")),
                max_hotwords=int(getattr(args, "auto_lexicon_max_hotwords", 40)),
                min_accepts=int(getattr(args, "auto_lexicon_min_accepts", 2)),
                max_terms=int(getattr(args, "auto_lexicon_max_terms", 5000)),
            )
            if args.debug_diagnostics:
                on_state(
                    {
                        "event": "log",
                        "message": (
                            "diag auto_lexicon enabled"
                            f" db={str(Path(getattr(args, 'auto_lexicon_db', '')).expanduser())}"
                            f" min_accepts={int(getattr(args, 'auto_lexicon_min_accepts', 2))}"
                            f" max_hotwords={int(getattr(args, 'auto_lexicon_max_hotwords', 40))}"
                        ),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            auto_lexicon = None
            on_state({"event": "log", "message": f"auto_lexicon_disabled: {type(exc).__name__}: {exc}"})

    def _resolve_hotwords() -> list[str]:
        base_hotwords = list(getattr(args, "hotword", []))
        if auto_lexicon is None:
            return base_hotwords
        try:
            return auto_lexicon.compose_hotwords(base_hotwords)
        except Exception as exc:  # noqa: BLE001
            if args.debug_diagnostics:
                on_state({"event": "log", "message": f"diag auto_lexicon_compose_failed: {exc}"})
            return base_hotwords

    # Initialize text refiner if enabled
    from .providers.base_text_refiner import BaseTextRefiner

    refiner: BaseTextRefiner | None = None
    refine_postprocess_rule = "none"
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
        refine_postprocess_rule, custom_prompt = _extract_refine_postprocess_rule(custom_prompt)
        if args.debug_diagnostics:
            on_state({"event": "log", "message": f"diag refine_postprocess_rule={refine_postprocess_rule}"})

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
            try:
                refiner.refine("测试")
            except Exception as exc:  # noqa: BLE001
                on_state(
                    {
                        "event": "refiner_warmup",
                        "status": "failed",
                        "provider": refiner.provider_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                on_state({"event": "log", "message": f"refiner_warmup_failed: {type(exc).__name__}: {exc}"})
            else:
                on_state(
                    {
                        "event": "refiner_warmup",
                        "status": "ready",
                        "provider": refiner.provider_name,
                        "latency_ms": (time.perf_counter() - t0) * 1000,
                    }
                )

    state_lock = threading.RLock()
    state: dict[str, object] = {
        "last_trigger": 0.0,
        "process": None,
        "temp_dir": None,
        "audio_path": None,
        "record_started_at": None,
        "target_window_id": None,
        "level_stop": None,  # threading.Event to stop audio level sampling
        "processing_thread": None,
        "recording_state": RecordingState.IDLE,
        "record_source": "hotkey",
        "voice_session_active": False,
        "voice_last_speech_ts": 0.0,
        "voice_started_ts": 0.0,
        "voice_speech_detected": False,
        "voice_auto_stopping": False,
        "voice_semantic_enabled": False,
        "voice_semantic_has_text": False,
        "voice_semantic_last_text_ts": 0.0,
        "voice_semantic_last_text": "",
        "voice_owner_filter_enabled": False,
        "voice_owner_active": True,
        "voice_owner_seen": False,
        "voice_owner_last_score": -1.0,
    }

    def _get_state(key: str) -> object:
        """线程安全地读取状态"""
        with state_lock:
            return state.get(key)

    def _set_state(key: str, value: object) -> None:
        """线程安全地设置状态"""
        with state_lock:
            state[key] = value

    def _update_state(updates: dict[str, object]) -> None:
        """线程安全地批量更新状态"""
        with state_lock:
            state.update(updates)

    def _transition_to_idle() -> None:
        _update_state({
            "process": None,
            "temp_dir": None,
            "audio_path": None,
            "record_started_at": None,
            "level_stop": None,
            "recording_state": RecordingState.IDLE,
            "record_source": "hotkey",
            "voice_session_active": False,
            "voice_last_speech_ts": 0.0,
            "voice_started_ts": 0.0,
            "voice_speech_detected": False,
            "voice_auto_stopping": False,
            "voice_semantic_enabled": False,
            "voice_semantic_has_text": False,
            "voice_semantic_last_text_ts": 0.0,
            "voice_semantic_last_text": "",
            "voice_owner_filter_enabled": False,
            "voice_owner_active": True,
            "voice_owner_seen": False,
            "voice_owner_last_score": -1.0,
        })

    def _wait_for_processing_completion() -> None:
        processing_thread = _get_state("processing_thread")
        if (
            isinstance(processing_thread, threading.Thread)
            and processing_thread.is_alive()
            and processing_thread is not threading.current_thread()
        ):
            processing_thread.join()

    def _start_recording(trigger_source: str = "hotkey") -> bool:
        now = time.monotonic()
        last_trigger = float(cast(float, _get_state("last_trigger")))
        if now - last_trigger < cooldown_s:
            return False
        _set_state("last_trigger", now)

        target_wid = get_focused_window_id()
        _set_state("target_window_id", target_wid)
        _apply_target_window(committer, {"target_window_id": target_wid})
        if args.debug_diagnostics:
            on_state(
                {
                    "event": "log",
                    "message": (
                        f"diag capture target_window_id={target_wid}"
                        f" commit_backend={getattr(committer, 'backend_name', 'unknown')}"
                    ),
                }
            )

        if not lock.acquire(blocking=False):
            on_busy({"event": "busy", "reason": "dictation_in_progress"})
            return False

        temp_dir: TemporaryDirectory[str] | None = None
        try:
            _set_state("recording_state", RecordingState.RECORDING)
            temp_dir = TemporaryDirectory(prefix="recordian-ptt-")
            suffix = ".ogg" if args.record_format == "ogg" else ".wav"
            if recorder_backend == "arecord":
                suffix = ".wav"
            audio_path = Path(temp_dir.name) / f"input{suffix}"
            record_handle = start_record_process(
                args=args,
                ffmpeg_bin=ffmpeg_bin,
                recorder_backend=recorder_backend,
                output_path=audio_path,
                duration_s=None,
                enable_monitor=True,
            )
            _update_state({
                "process": record_handle,
                "temp_dir": temp_dir,
                "audio_path": audio_path,
                "record_started_at": time.perf_counter(),
                "record_source": trigger_source,
                "voice_session_active": trigger_source == "voice_wake",
                "voice_last_speech_ts": time.monotonic(),
                "voice_started_ts": time.monotonic(),
                "voice_speech_detected": False,
                "voice_auto_stopping": False,
                "voice_semantic_enabled": trigger_source == "voice_wake" and bool(getattr(args, "wake_use_semantic_gate", False)),
                "voice_semantic_has_text": False,
                "voice_semantic_last_text_ts": 0.0,
                "voice_semantic_last_text": "",
                "voice_owner_filter_enabled": trigger_source == "voice_wake" and bool(getattr(args, "wake_owner_verify", False)),
                # Optimistically allow the first speech frames after wake trigger.
                # Owner verification can still deactivate the session once there
                # is enough audio to make a real decision.
                "voice_owner_active": True,
                "voice_owner_seen": False,
                "voice_owner_last_score": -1.0,
                "realtime_asr_worker": None,
            })
            on_state({"event": "recording_started", "record_backend": recorder_backend, "audio_path": str(audio_path)})

            # Start audio level sampling thread
            level_stop = threading.Event()
            _set_state("level_stop", level_stop)
            # Security invariant lives in wake_session_monitor: owner_audio_chunks uses deque(maxlen=100).
            start_wake_session_monitor(
                WakeSessionMonitorContext(
                    args=args,
                    record_handle=record_handle,
                    provider=provider,
                    stop_event=level_stop,
                    get_state=_get_state,
                    set_state=_set_state,
                    resolve_hotwords=_resolve_hotwords,
                    stop_recording=_stop_recording,
                    normalize_final_text=_normalize_final_text,
                    on_state=on_state,
                )
            )
            routing = resolve_remote_paste_routing(args)
            realtime_worker = _start_realtime_asr_worker(
                args=args,
                provider=provider,
                record_handle=record_handle,
                committer=committer,
                enable_local_commit=bool(routing.commit_local),
                auto_hard_enter=_resolve_auto_hard_enter(args),
                resolve_hotwords=_resolve_hotwords,
                normalize_final_text=_normalize_final_text,
                on_state=on_state,
            )
            if realtime_worker is not None:
                _set_state("realtime_asr_worker", realtime_worker)
            return True
        except Exception:  # noqa: BLE001
            # 确保在异常路径停止音频采样线程
            level_stop_val: object = _get_state("level_stop")
            if isinstance(level_stop_val, threading.Event):
                level_stop_val.set()
            if temp_dir is not None:
                temp_dir.cleanup()
            _transition_to_idle()
            lock.release()
            raise

    def _stop_recording() -> bool:
        with state_lock:
            process = state.get("process")
            started = state.get("record_started_at")
            audio_path = state.get("audio_path")
            temp_dir = state.get("temp_dir")
            level_stop = state.get("level_stop")
            owner_filter_enabled = bool(state.get("voice_owner_filter_enabled"))
            owner_seen = bool(state.get("voice_owner_seen"))
            try:
                owner_last_score = float(cast(float, state.get("voice_owner_last_score")))
            except Exception:
                owner_last_score = -1.0
            realtime_asr_worker = state.get("realtime_asr_worker")
            if process is None or audio_path is None or temp_dir is None or started is None:
                return False

            # Narrow dict[str, object] values to expected types after None-guard.
            _process = cast(RecordProcessHandle | subprocess.Popen[Any], process)
            _started = cast(float, started)
            _audio_path = cast(Path, audio_path)
            _temp_dir = cast(TemporaryDirectory[str], temp_dir)

            state.update({
                "process": None,
                "audio_path": None,
                "temp_dir": None,
                "record_started_at": None,
                "level_stop": None,
                "recording_state": RecordingState.PROCESSING,
                "voice_session_active": False,
                "voice_auto_stopping": False,
                "voice_semantic_enabled": False,
                "voice_semantic_has_text": False,
                "voice_semantic_last_text_ts": 0.0,
                "voice_semantic_last_text": "",
                "voice_owner_filter_enabled": False,
                "voice_owner_active": True,
                "voice_owner_seen": False,
                "voice_owner_last_score": -1.0,
                "realtime_asr_worker": None,
            })

        if isinstance(level_stop, threading.Event):
            level_stop.set()

        try:
            stop_record_process(_process, recorder_backend=recorder_backend)
        except Exception as exc:  # noqa: BLE001
            _temp_dir.cleanup()
            _transition_to_idle()
            lock.release()
            on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            return False

        record_latency_ms = (time.perf_counter() - _started) * 1000
        audio_path = Path(_audio_path)
        on_state({"event": "processing_started", "record_backend": recorder_backend, "audio_path": str(audio_path), "record_latency_ms": record_latency_ms})

        def _worker() -> None:
            try:
                realtime_final_text = ""
                realtime_detected_language = ""
                realtime_transcribe_latency_ms = 0.0
                realtime_commit_info: dict[str, object] | None = None
                if isinstance(realtime_asr_worker, _RealtimeASRWorkerHandle):
                    realtime_asr_worker.thread.join(timeout=5.0)
                    if realtime_asr_worker.error:
                        on_state({"event": "log", "message": f"realtime_asr_failed: {realtime_asr_worker.error}"})
                    else:
                        realtime_final_text = realtime_asr_worker.final_text
                        realtime_detected_language = realtime_asr_worker.detected_language
                        realtime_transcribe_latency_ms = realtime_asr_worker.transcribe_latency_ms
                        if isinstance(realtime_asr_worker.commit_info, dict):
                            realtime_commit_info = realtime_asr_worker.commit_info
                run_postprocess_pipeline(
                    PostprocessPipelineContext(
                        args=args,
                        audio_path=audio_path,
                        record_backend=recorder_backend,
                        record_latency_ms=record_latency_ms,
                        owner_filter_enabled=owner_filter_enabled,
                        owner_seen=owner_seen,
                        owner_last_score=owner_last_score,
                        state=state,
                        provider=provider,
                        refiner=refiner,
                        committer=committer,
                        auto_lexicon=auto_lexicon,
                        refine_postprocess_rule=refine_postprocess_rule,
                        normalize_final_text=_normalize_final_text,
                        resolve_hotwords=_resolve_hotwords,
                        prefetched_asr_text=realtime_final_text,
                        prefetched_detected_language=realtime_detected_language,
                        prefetched_transcribe_latency_ms=realtime_transcribe_latency_ms,
                        prefetched_commit_info=realtime_commit_info,
                        on_state=on_state,
                        on_result=on_result,
                        on_error=on_error,
                    )
                )
            finally:
                _temp_dir.cleanup()
                _set_state("processing_thread", None)
                _set_state("recording_state", RecordingState.IDLE)
                lock.release()

        processing_thread = threading.Thread(target=_worker, name="recordian-postprocess")
        _set_state("processing_thread", processing_thread)
        processing_thread.start()
        return True

    def _exit() -> None:
        _stop_recording()
        _wait_for_processing_completion()
        stop_event.set()

    return _start_recording, _stop_recording, _exit, stop_event
