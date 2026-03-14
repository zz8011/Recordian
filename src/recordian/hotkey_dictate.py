from __future__ import annotations

import argparse
import enum
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from recordian.config import ConfigManager
from recordian.runtime_config import apply_namespace_runtime_normalization, normalize_runtime_config

from .audio_feedback import default_sound_off_path, default_sound_on_path, play_sound
from .auto_lexicon import AutoLexicon
from .linux_commit import get_focused_window_id, resolve_committer, send_hard_enter
from .linux_dictate import (
    add_dictate_args,
    choose_record_backend,
    create_provider,
    run_dictate_once,
    start_record_process,
    stop_record_process,
)
from .linux_notify import Notification, resolve_notifier
from .postprocess_pipeline import (
    PostprocessPipelineContext,
    run_postprocess_pipeline,
)
from .postprocess_pipeline import (
    _apply_refine_postprocess as _pipeline_apply_refine_postprocess,
)
from .postprocess_pipeline import (
    _apply_target_window as _pipeline_apply_target_window,
)
from .postprocess_pipeline import (
    _build_refine_prompt_with_protected_terms as _pipeline_build_refine_prompt_with_protected_terms,
)
from .postprocess_pipeline import (
    _cleanup_repeat_lite_text as _pipeline_cleanup_repeat_lite_text,
)
from .postprocess_pipeline import (
    _cleanup_stutter_text as _pipeline_cleanup_stutter_text,
)
from .postprocess_pipeline import (
    _coerce_bool as _pipeline_coerce_bool,
)
from .postprocess_pipeline import (
    _extract_refine_postprocess_rule as _pipeline_extract_refine_postprocess_rule,
)
from .postprocess_pipeline import (
    _preview_text as _pipeline_preview_text,
)
from .postprocess_pipeline import (
    _resolve_auto_hard_enter as _pipeline_resolve_auto_hard_enter,
)
from .postprocess_pipeline import (
    _select_refine_protected_terms as _pipeline_select_refine_protected_terms,
)
from .postprocess_pipeline import (
    _should_skip_owner_gated_asr as _pipeline_should_skip_owner_gated_asr,
)
from .postprocess_pipeline import (
    _text_contains_term as _pipeline_text_contains_term,
)
from .runtime_deps import ensure_ffmpeg_available
from .voice_wake import (
    DEFAULT_WAKE_KEYWORD_THRESHOLD,
    DEFAULT_WAKE_NUM_THREADS,
    VoiceWakeService,
    make_wake_model_config,
    make_wake_runtime_config,
)
from .wake_session_monitor import (
    WakeSessionMonitorContext,
    start_wake_session_monitor,
)
from .wake_session_monitor import (
    _adaptive_vad_threshold as _monitor_adaptive_vad_threshold,
)
from .wake_session_monitor import (
    _float_to_pcm16le as _monitor_float_to_pcm16le,
)
from .wake_session_monitor import (
    _is_level_speech_frame as _monitor_is_level_speech_frame,
)
from .wake_session_monitor import (
    _owner_gate_level as _monitor_owner_gate_level,
)
from .wake_session_monitor import (
    _pcm16le_to_f32 as _monitor_pcm16le_to_f32,
)
from .wake_session_monitor import (
    _pick_vad_sample_rate as _monitor_pick_vad_sample_rate,
)
from .wake_session_monitor import (
    _resample_audio_for_vad as _monitor_resample_audio_for_vad,
)
from .wake_session_monitor import (
    _semantic_probe_text as _monitor_semantic_probe_text,
)
from .wake_session_monitor import (
    _semantic_text_has_content as _monitor_semantic_text_has_content,
)
from .wake_session_monitor import (
    _semantic_text_signal_len as _monitor_semantic_text_signal_len,
)
from .wake_session_monitor import (
    _should_auto_stop_semantic_session as _monitor_should_auto_stop_semantic_session,
)
from .wake_session_monitor import (
    _update_speech_evidence as _monitor_update_speech_evidence,
)
from .wake_session_monitor import (
    _vad_frame_bytes as _monitor_vad_frame_bytes,
)

DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"

_DEFAULT_WAKE_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
_DEFAULT_WAKE_ENCODER = _DEFAULT_WAKE_MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEFAULT_WAKE_DECODER = _DEFAULT_WAKE_MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEFAULT_WAKE_JOINER = _DEFAULT_WAKE_MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEFAULT_WAKE_TOKENS = _DEFAULT_WAKE_MODEL_DIR / "tokens.txt"
_DEFAULT_SOUND_ON = default_sound_on_path()
_DEFAULT_SOUND_OFF = default_sound_off_path()

_extract_refine_postprocess_rule = _pipeline_extract_refine_postprocess_rule
_cleanup_stutter_text = _pipeline_cleanup_stutter_text
_cleanup_repeat_lite_text = _pipeline_cleanup_repeat_lite_text
_apply_refine_postprocess = _pipeline_apply_refine_postprocess
_text_contains_term = _pipeline_text_contains_term
_select_refine_protected_terms = _pipeline_select_refine_protected_terms
_build_refine_prompt_with_protected_terms = _pipeline_build_refine_prompt_with_protected_terms
_preview_text = _pipeline_preview_text
_coerce_bool = _pipeline_coerce_bool
_resolve_auto_hard_enter = _pipeline_resolve_auto_hard_enter
_apply_target_window = _pipeline_apply_target_window
_should_skip_owner_gated_asr = _pipeline_should_skip_owner_gated_asr
_pick_vad_sample_rate = _monitor_pick_vad_sample_rate
_vad_frame_bytes = _monitor_vad_frame_bytes
_float_to_pcm16le = _monitor_float_to_pcm16le
_resample_audio_for_vad = _monitor_resample_audio_for_vad
_adaptive_vad_threshold = _monitor_adaptive_vad_threshold
_is_level_speech_frame = _monitor_is_level_speech_frame
_update_speech_evidence = _monitor_update_speech_evidence
_owner_gate_level = _monitor_owner_gate_level
_semantic_text_signal_len = _monitor_semantic_text_signal_len
_semantic_text_has_content = _monitor_semantic_text_has_content
_semantic_probe_text = _monitor_semantic_probe_text
_should_auto_stop_semantic_session = _monitor_should_auto_stop_semantic_session
_pcm16le_to_f32 = _monitor_pcm16le_to_f32


class RecordingState(enum.Enum):
    """录音状态枚举"""
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


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
        choices=["local", "cloud", "llamacpp"],
        default="local",
        help="Text refinement provider: local (Qwen3-0.6B), cloud (API), or llamacpp (GGUF)",
    )
    parser.add_argument(
        "--refine-n-gpu-layers",
        type=int,
        default=-1,
        help="Number of GPU layers for llama.cpp refiner (-1 = all)",
    )
    parser.add_argument(
        "--enable-thinking",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable reasoning mode in text refiner when provider supports it",
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
    parser.add_argument(
        "--enable-voice-wake",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable wake-word mode (continuous low-power listening)",
    )
    parser.add_argument(
        "--wake-prefix",
        action="append",
        default=["嗨", "嘿"],
        help="Wake-word prefix, repeatable (e.g., 嗨/嘿)",
    )
    parser.add_argument(
        "--wake-name",
        action="append",
        default=["小二"],
        help="Wake-word name, repeatable (e.g., 小二/小三/乐乐)",
    )
    parser.add_argument("--wake-cooldown-s", type=float, default=3.0, help="Cooldown after wake trigger")
    parser.add_argument("--wake-auto-stop-silence-s", type=float, default=1.5, help="Auto-stop after silence")
    parser.add_argument(
        "--wake-owner-silence-extend-s",
        type=float,
        default=0.5,
        help="Additional silence time for owner voice (0.0-5.0 seconds)",
    )
    parser.add_argument("--wake-min-speech-s", type=float, default=0.5, help="Minimum speech duration before auto-stop")
    parser.add_argument(
        "--wake-use-webrtcvad",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use WebRTC VAD for wake-session speech/silence detection",
    )
    parser.add_argument(
        "--wake-vad-aggressiveness",
        type=int,
        default=2,
        choices=[0, 1, 2, 3],
        help="WebRTC VAD aggressiveness (0=loose, 3=strict)",
    )
    parser.add_argument(
        "--wake-vad-frame-ms",
        type=int,
        default=30,
        choices=[10, 20, 30],
        help="WebRTC VAD frame size in ms",
    )
    parser.add_argument(
        "--wake-no-speech-timeout-s",
        type=float,
        default=2.0,
        help="Auto-stop if no speech is detected after wake",
    )
    parser.add_argument(
        "--wake-speech-confirm-s",
        type=float,
        default=0.18,
        help="Required speech evidence time before considering voice as started",
    )
    parser.add_argument(
        "--wake-stats",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Emit wake loop profiling stats events (for CPU baseline measurement)",
    )
    parser.add_argument(
        "--wake-pre-vad",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Gate wake KWS decode by WebRTC VAD in standby mode",
    )
    parser.add_argument(
        "--wake-pre-vad-aggressiveness",
        type=int,
        default=3,
        choices=[0, 1, 2, 3],
        help="WebRTC VAD aggressiveness for wake standby gating (0=loose, 3=strict)",
    )
    parser.add_argument(
        "--wake-pre-vad-frame-ms",
        type=int,
        default=30,
        choices=[10, 20, 30],
        help="WebRTC VAD frame size in ms for wake standby gating",
    )
    parser.add_argument(
        "--wake-pre-vad-enter-frames",
        type=int,
        default=4,
        help="Consecutive speech frames needed to open wake KWS gate",
    )
    parser.add_argument(
        "--wake-pre-vad-hangover-ms",
        type=int,
        default=120,
        help="Keep wake KWS gate open for this long after last speech frame",
    )
    parser.add_argument(
        "--wake-pre-roll-ms",
        type=int,
        default=300,
        help="Audio pre-roll sent to KWS when wake gate opens",
    )
    parser.add_argument(
        "--wake-decode-budget-per-cycle",
        type=int,
        default=1,
        help="Max wake KWS decode calls per audio read cycle",
    )
    parser.add_argument(
        "--wake-decode-budget-per-sec",
        type=float,
        default=16.0,
        help="Token-bucket budget for wake KWS decode calls per second",
    )
    parser.add_argument(
        "--wake-auto-name-variants",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-expand tone/homophone token variants for configured wake names (generic, not hardcoded words)",
    )
    parser.add_argument(
        "--wake-auto-prefix-variants",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-expand common homophone variants for wake prefixes (e.g. 嘿 -> 嗨/黑)",
    )
    parser.add_argument(
        "--wake-allow-name-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also allow wake by name-only phrase (e.g. 小二) to reduce clipped-prefix misses",
    )
    parser.add_argument(
        "--wake-owner-verify",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Require registered owner voiceprint verification after keyword hit",
    )
    parser.add_argument(
        "--wake-owner-profile",
        default="~/.config/recordian/owner_voice_profile.json",
        help="Owner voiceprint feature profile json path",
    )
    parser.add_argument(
        "--wake-owner-sample",
        default="",
        help="Optional owner sample wav path for auto-enrollment when profile is missing",
    )
    parser.add_argument(
        "--wake-owner-threshold",
        type=float,
        default=0.72,
        help="Owner voiceprint cosine threshold (0~1, higher = stricter)",
    )
    parser.add_argument(
        "--wake-owner-window-s",
        type=float,
        default=1.6,
        help="Audio window length (seconds) used for owner voiceprint verification",
    )
    parser.add_argument(
        "--wake-use-semantic-gate",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Use lightweight semantic probe (text presence) as side-channel for wake session start/end",
    )
    parser.add_argument(
        "--wake-semantic-probe-interval-s",
        type=float,
        default=0.45,
        help="Semantic probe interval in seconds",
    )
    parser.add_argument(
        "--wake-semantic-window-s",
        type=float,
        default=1.2,
        help="Recent audio window length in seconds for each semantic probe",
    )
    parser.add_argument(
        "--wake-semantic-end-silence-s",
        type=float,
        default=1.5,
        help="Auto-stop if semantic probe sees no text growth for this duration",
    )
    parser.add_argument(
        "--wake-semantic-min-chars",
        type=int,
        default=1,
        help="Minimum effective chars to consider semantic speech detected",
    )
    parser.add_argument(
        "--wake-semantic-timeout-ms",
        type=int,
        default=1200,
        help="Timeout for each semantic probe ASR call",
    )
    parser.add_argument("--sound-on-path", default=str(_DEFAULT_SOUND_ON), help="Global cue sound when recording starts")
    parser.add_argument("--sound-off-path", default=str(_DEFAULT_SOUND_OFF), help="Global cue sound when recording ends")
    parser.add_argument("--wake-beep-path", default="", help="Deprecated legacy cue path, kept for compatibility")
    parser.add_argument("--wake-encoder", default=str(_DEFAULT_WAKE_ENCODER))
    parser.add_argument("--wake-decoder", default=str(_DEFAULT_WAKE_DECODER))
    parser.add_argument("--wake-joiner", default=str(_DEFAULT_WAKE_JOINER))
    parser.add_argument("--wake-tokens", default=str(_DEFAULT_WAKE_TOKENS))
    parser.add_argument("--wake-keywords-file", default="", help="Optional pre-tokenized keywords.txt path")
    parser.add_argument("--wake-tokens-type", default="ppinyin", choices=["ppinyin", "bpe", "cjkchar", "fpinyin"])
    parser.add_argument("--wake-provider", default="cpu")
    parser.add_argument("--wake-num-threads", type=int, default=DEFAULT_WAKE_NUM_THREADS)
    parser.add_argument("--wake-sample-rate", type=int, default=16000)
    parser.add_argument("--wake-keyword-score", type=float, default=1.5)
    parser.add_argument("--wake-keyword-threshold", type=float, default=DEFAULT_WAKE_KEYWORD_THRESHOLD)
    parser.add_argument(
        "--enable-auto-lexicon",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-learn personal hotwords from accepted committed text",
    )
    parser.add_argument(
        "--auto-lexicon-db",
        default="~/.config/recordian/auto_lexicon.db",
        help="SQLite path for auto hotword lexicon",
    )
    parser.add_argument(
        "--auto-lexicon-max-hotwords",
        type=int,
        default=40,
        help="Max total hotwords sent to ASR (manual + auto)",
    )
    parser.add_argument(
        "--auto-lexicon-min-accepts",
        type=int,
        default=2,
        help="Minimum accepted occurrences before a learned term is used",
    )
    parser.add_argument(
        "--auto-lexicon-max-terms",
        type=int,
        default=5000,
        help="Max learned terms retained in local lexicon",
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
    refiner = None
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
        last_trigger = float(_get_state("last_trigger"))
        if now - last_trigger < cooldown_s:
            return False
        _set_state("last_trigger", now)

        target_wid = get_focused_window_id()
        _set_state("target_window_id", target_wid)
        if args.debug_diagnostics:
            on_state({"event": "log", "message": f"diag capture target_window_id={target_wid}"})

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
                "voice_owner_active": not (trigger_source == "voice_wake" and bool(getattr(args, "wake_owner_verify", False))),
                "voice_owner_seen": False,
                "voice_owner_last_score": -1.0,
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
            return True
        except Exception:  # noqa: BLE001
            # 确保在异常路径停止音频采样线程
            level_stop = _get_state("level_stop")
            if isinstance(level_stop, threading.Event):
                level_stop.set()
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
                owner_last_score = float(state.get("voice_owner_last_score"))
            except Exception:
                owner_last_score = -1.0
            if process is None or audio_path is None or temp_dir is None or started is None:
                return False

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
            })

        if isinstance(level_stop, threading.Event):
            level_stop.set()

        try:
            stop_record_process(process, recorder_backend=recorder_backend)
        except Exception as exc:  # noqa: BLE001
            temp_dir.cleanup()
            _transition_to_idle()
            lock.release()
            on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            return False

        record_latency_ms = (time.perf_counter() - float(started)) * 1000
        audio_path = Path(audio_path)
        on_state({"event": "processing_started", "record_backend": recorder_backend, "audio_path": str(audio_path), "record_latency_ms": record_latency_ms})

        def _worker() -> None:
            try:
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
                        on_state=on_state,
                        on_result=on_result,
                        on_error=on_error,
                    )
                )
            finally:
                temp_dir.cleanup()
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


def _parse_args_with_config(parser: argparse.ArgumentParser) -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    pre.add_argument("--no-load-config", action="store_true")
    pre_args, _ = pre.parse_known_args()

    config_path = Path(pre_args.config_path).expanduser()
    if not pre_args.no_load_config and config_path.exists():
        payload = ConfigManager.load(config_path)
        if isinstance(payload, dict):
            # Backward-compat config normalization.
            defaults_payload = normalize_runtime_config(payload, config_base_dir=config_path.parent)
            if "enable_thinking" not in defaults_payload and "refine_enable_thinking" in defaults_payload:
                defaults_payload["enable_thinking"] = defaults_payload.get("refine_enable_thinking")
            if not defaults_payload.get("refine_model") and defaults_payload.get("refine_model_llamacpp"):
                defaults_payload["refine_model"] = defaults_payload.get("refine_model_llamacpp")

            allowed = {
                action.dest
                for action in parser._actions
                if action.dest not in {"help", "save_config", "no_load_config"}
            }
            defaults = {k: v for k, v in defaults_payload.items() if k in allowed}
            if defaults:
                parser.set_defaults(**defaults)

    args = parser.parse_args()
    # Guard against invalid legacy values that may slip through argparse defaults.
    apply_namespace_runtime_normalization(
        args,
        allow_auto_fallback_commit=True,
        config_base_dir=config_path.parent,
    )
    args.auto_hard_enter = bool(getattr(args, "auto_hard_enter", False))
    try:
        wake_vad_aggressiveness = int(getattr(args, "wake_vad_aggressiveness", 2))
    except Exception:
        wake_vad_aggressiveness = 2
    if wake_vad_aggressiveness not in {0, 1, 2, 3}:
        args.wake_vad_aggressiveness = 2
    else:
        args.wake_vad_aggressiveness = wake_vad_aggressiveness
    try:
        wake_vad_frame_ms = int(getattr(args, "wake_vad_frame_ms", 30))
    except Exception:
        wake_vad_frame_ms = 30
    if wake_vad_frame_ms not in {10, 20, 30}:
        args.wake_vad_frame_ms = 30
    else:
        args.wake_vad_frame_ms = wake_vad_frame_ms
    try:
        args.wake_no_speech_timeout_s = max(0.0, float(getattr(args, "wake_no_speech_timeout_s", 2.0)))
    except Exception:
        args.wake_no_speech_timeout_s = 2.0
    try:
        args.wake_speech_confirm_s = max(0.0, float(getattr(args, "wake_speech_confirm_s", 0.18)))
    except Exception:
        args.wake_speech_confirm_s = 0.18
    args.wake_pre_vad = _coerce_bool(getattr(args, "wake_pre_vad", True), default=True)
    try:
        wake_pre_vad_aggr = int(getattr(args, "wake_pre_vad_aggressiveness", 3))
    except Exception:
        wake_pre_vad_aggr = 3
    if wake_pre_vad_aggr not in {0, 1, 2, 3}:
        args.wake_pre_vad_aggressiveness = 3
    else:
        args.wake_pre_vad_aggressiveness = wake_pre_vad_aggr
    try:
        wake_pre_vad_frame_ms = int(getattr(args, "wake_pre_vad_frame_ms", 30))
    except Exception:
        wake_pre_vad_frame_ms = 30
    if wake_pre_vad_frame_ms not in {10, 20, 30}:
        args.wake_pre_vad_frame_ms = 30
    else:
        args.wake_pre_vad_frame_ms = wake_pre_vad_frame_ms
    try:
        args.wake_pre_vad_enter_frames = max(1, int(getattr(args, "wake_pre_vad_enter_frames", 4)))
    except Exception:
        args.wake_pre_vad_enter_frames = 4
    try:
        args.wake_pre_vad_hangover_ms = max(0, int(getattr(args, "wake_pre_vad_hangover_ms", 120)))
    except Exception:
        args.wake_pre_vad_hangover_ms = 120
    try:
        args.wake_pre_roll_ms = max(0, int(getattr(args, "wake_pre_roll_ms", 300)))
    except Exception:
        args.wake_pre_roll_ms = 300
    try:
        args.wake_decode_budget_per_cycle = max(1, int(getattr(args, "wake_decode_budget_per_cycle", 1)))
    except Exception:
        args.wake_decode_budget_per_cycle = 1
    try:
        args.wake_decode_budget_per_sec = max(1.0, float(getattr(args, "wake_decode_budget_per_sec", 16.0)))
    except Exception:
        args.wake_decode_budget_per_sec = 16.0
    args.wake_auto_name_variants = _coerce_bool(getattr(args, "wake_auto_name_variants", True), default=True)
    args.wake_auto_prefix_variants = _coerce_bool(getattr(args, "wake_auto_prefix_variants", True), default=True)
    args.wake_allow_name_only = _coerce_bool(getattr(args, "wake_allow_name_only", True), default=True)
    args.wake_stats = _coerce_bool(getattr(args, "wake_stats", False), default=False)
    args.wake_owner_verify = _coerce_bool(getattr(args, "wake_owner_verify", False), default=False)
    try:
        args.wake_owner_threshold = min(0.99, max(0.0, float(getattr(args, "wake_owner_threshold", 0.72))))
    except Exception:
        args.wake_owner_threshold = 0.72
    try:
        args.wake_owner_window_s = max(0.6, float(getattr(args, "wake_owner_window_s", 1.6)))
    except Exception:
        args.wake_owner_window_s = 1.6
    args.wake_use_semantic_gate = _coerce_bool(getattr(args, "wake_use_semantic_gate", False), default=False)
    try:
        args.wake_semantic_probe_interval_s = max(0.1, float(getattr(args, "wake_semantic_probe_interval_s", 0.45)))
    except Exception:
        args.wake_semantic_probe_interval_s = 0.45
    try:
        args.wake_semantic_window_s = max(0.4, float(getattr(args, "wake_semantic_window_s", 1.2)))
    except Exception:
        args.wake_semantic_window_s = 1.2
    try:
        args.wake_semantic_end_silence_s = max(0.2, float(getattr(args, "wake_semantic_end_silence_s", 1.5)))
    except Exception:
        args.wake_semantic_end_silence_s = 1.0
    try:
        args.wake_semantic_min_chars = max(1, int(getattr(args, "wake_semantic_min_chars", 1)))
    except Exception:
        args.wake_semantic_min_chars = 1
    try:
        args.wake_semantic_timeout_ms = max(200, int(getattr(args, "wake_semantic_timeout_ms", 1200)))
    except Exception:
        args.wake_semantic_timeout_ms = 1200
    args.enable_auto_lexicon = _coerce_bool(getattr(args, "enable_auto_lexicon", True), default=True)
    try:
        args.auto_lexicon_max_hotwords = max(0, int(getattr(args, "auto_lexicon_max_hotwords", 40)))
    except Exception:
        args.auto_lexicon_max_hotwords = 40
    try:
        args.auto_lexicon_min_accepts = max(1, int(getattr(args, "auto_lexicon_min_accepts", 2)))
    except Exception:
        args.auto_lexicon_min_accepts = 2
    try:
        args.auto_lexicon_max_terms = max(100, int(getattr(args, "auto_lexicon_max_terms", 5000)))
    except Exception:
        args.auto_lexicon_max_terms = 5000
    args.config_path = str(Path(args.config_path).expanduser())
    return args


def _save_runtime_config(args: argparse.Namespace) -> None:
    wake_runtime = make_wake_runtime_config(args)
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
        "auto_hard_enter": bool(getattr(args, "auto_hard_enter", False)),
        "enable_remote_paste": bool(getattr(args, "enable_remote_paste", False)),
        "remote_paste_host": getattr(args, "remote_paste_host", ""),
        "remote_paste_port": getattr(args, "remote_paste_port", 24872),
        "remote_paste_timeout_s": getattr(args, "remote_paste_timeout_s", 3.0),
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
        "refine_n_gpu_layers": getattr(args, "refine_n_gpu_layers", -1),
        "refine_max_tokens": getattr(args, "refine_max_tokens", 512),
        "enable_thinking": getattr(args, "enable_thinking", False),
        "refine_prompt": getattr(args, "refine_prompt", ""),
        "refine_preset": getattr(args, "refine_preset", "default"),
        "refine_api_base": getattr(args, "refine_api_base", "https://api.minimaxi.com/anthropic"),
        "refine_api_key": getattr(args, "refine_api_key", ""),
        "refine_api_model": getattr(args, "refine_api_model", "claude-3-5-sonnet-20241022"),
        "enable_streaming_refine": getattr(args, "enable_streaming_refine", False),
        "enable_voice_wake": getattr(args, "enable_voice_wake", False),
        "wake_prefix": wake_runtime.prefixes,
        "wake_name": wake_runtime.names,
        "wake_cooldown_s": getattr(args, "wake_cooldown_s", 3.0),
        "wake_auto_stop_silence_s": getattr(args, "wake_auto_stop_silence_s", 1.5),
        "wake_min_speech_s": getattr(args, "wake_min_speech_s", 0.5),
        "wake_use_webrtcvad": getattr(args, "wake_use_webrtcvad", True),
        "wake_vad_aggressiveness": getattr(args, "wake_vad_aggressiveness", 2),
        "wake_vad_frame_ms": getattr(args, "wake_vad_frame_ms", 30),
        "wake_no_speech_timeout_s": getattr(args, "wake_no_speech_timeout_s", 2.0),
        "wake_speech_confirm_s": getattr(args, "wake_speech_confirm_s", 0.18),
        "wake_stats": getattr(args, "wake_stats", False),
        "wake_pre_vad": getattr(args, "wake_pre_vad", True),
        "wake_pre_vad_aggressiveness": getattr(args, "wake_pre_vad_aggressiveness", 3),
        "wake_pre_vad_frame_ms": getattr(args, "wake_pre_vad_frame_ms", 30),
        "wake_pre_vad_enter_frames": getattr(args, "wake_pre_vad_enter_frames", 4),
        "wake_pre_vad_hangover_ms": getattr(args, "wake_pre_vad_hangover_ms", 120),
        "wake_pre_roll_ms": getattr(args, "wake_pre_roll_ms", 300),
        "wake_decode_budget_per_cycle": getattr(args, "wake_decode_budget_per_cycle", 1),
        "wake_decode_budget_per_sec": getattr(args, "wake_decode_budget_per_sec", 16.0),
        "wake_auto_name_variants": getattr(args, "wake_auto_name_variants", True),
        "wake_auto_prefix_variants": getattr(args, "wake_auto_prefix_variants", True),
        "wake_allow_name_only": getattr(args, "wake_allow_name_only", True),
        "wake_owner_verify": getattr(args, "wake_owner_verify", False),
        "wake_owner_profile": getattr(args, "wake_owner_profile", "~/.config/recordian/owner_voice_profile.json"),
        "wake_owner_sample": getattr(args, "wake_owner_sample", ""),
        "wake_owner_threshold": getattr(args, "wake_owner_threshold", 0.72),
        "wake_owner_window_s": getattr(args, "wake_owner_window_s", 1.6),
        "wake_owner_silence_extend_s": max(0.0, min(5.0, float(getattr(args, "wake_owner_silence_extend_s", 0.5)))),
        "wake_use_semantic_gate": getattr(args, "wake_use_semantic_gate", False),
        "wake_semantic_probe_interval_s": getattr(args, "wake_semantic_probe_interval_s", 0.45),
        "wake_semantic_window_s": getattr(args, "wake_semantic_window_s", 1.2),
        "wake_semantic_end_silence_s": getattr(args, "wake_semantic_end_silence_s", 1.5),
        "wake_semantic_min_chars": getattr(args, "wake_semantic_min_chars", 1),
        "wake_semantic_timeout_ms": getattr(args, "wake_semantic_timeout_ms", 1200),
        "wake_provider": getattr(args, "wake_provider", "cpu"),
        "sound_on_path": getattr(args, "sound_on_path", str(_DEFAULT_SOUND_ON)),
        "sound_off_path": getattr(args, "sound_off_path", str(_DEFAULT_SOUND_OFF)),
        "wake_beep_path": getattr(args, "wake_beep_path", ""),
        "wake_encoder": getattr(args, "wake_encoder", str(_DEFAULT_WAKE_ENCODER)),
        "wake_decoder": getattr(args, "wake_decoder", str(_DEFAULT_WAKE_DECODER)),
        "wake_joiner": getattr(args, "wake_joiner", str(_DEFAULT_WAKE_JOINER)),
        "wake_tokens": getattr(args, "wake_tokens", str(_DEFAULT_WAKE_TOKENS)),
        "wake_keywords_file": getattr(args, "wake_keywords_file", ""),
        "wake_tokens_type": getattr(args, "wake_tokens_type", "ppinyin"),
        "wake_num_threads": getattr(args, "wake_num_threads", DEFAULT_WAKE_NUM_THREADS),
        "wake_sample_rate": getattr(args, "wake_sample_rate", 16000),
        "wake_energy_threshold": float(getattr(args, "wake_energy_threshold", 0.0001)),
        "wake_keyword_score": getattr(args, "wake_keyword_score", 1.5),
        "wake_keyword_threshold": getattr(args, "wake_keyword_threshold", DEFAULT_WAKE_KEYWORD_THRESHOLD),
        "enable_auto_lexicon": getattr(args, "enable_auto_lexicon", True),
        "auto_lexicon_db": getattr(args, "auto_lexicon_db", "~/.config/recordian/auto_lexicon.db"),
        "auto_lexicon_max_hotwords": getattr(args, "auto_lexicon_max_hotwords", 40),
        "auto_lexicon_min_accepts": getattr(args, "auto_lexicon_min_accepts", 2),
        "auto_lexicon_max_terms": getattr(args, "auto_lexicon_max_terms", 5000),
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
    import logging
    import sys

    from recordian.error_tracker import get_error_tracker

    logger = logging.getLogger(__name__)

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
        _main_impl()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(e)
        raise


def _main_impl() -> None:
    """Main implementation."""
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

    voice_wake_service: VoiceWakeService | None = None

    if args.trigger_mode in {"ptt", "toggle"}:
        start_recording, stop_recording, exit_daemon, stop_event = build_ptt_hotkey_handlers(
            args=args,
            on_result=_emit,
            on_error=_emit,
            on_busy=_emit,
            on_state=_emit,
        )

        if bool(getattr(args, "enable_voice_wake", False)):
            runtime_cfg = make_wake_runtime_config(args)
            model_cfg = make_wake_model_config(args)

            def _on_wake(keyword: str) -> None:
                try:
                    start_recording("voice_wake")
                except Exception as exc:  # noqa: BLE001
                    _emit({"event": "error", "error": f"voice_wake_start_failed: {exc}"})

            voice_wake_service = VoiceWakeService(
                model=model_cfg,
                runtime=runtime_cfg,
                on_wake=_on_wake,
                on_event=_emit,
                cache_dir=Path.home() / ".cache" / "recordian" / "wake",
            )
            voice_wake_service.start()
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
                        try:
                            toggle_recording["active"] = start_recording()
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
                        try:
                            recording["active"] = start_recording()
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
        if bool(getattr(args, "enable_voice_wake", False)):
            _emit({"event": "log", "message": "trigger_mode=oneshot 时不支持语音唤醒，已忽略"})
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
            "voice_wake_enabled": bool(getattr(args, "enable_voice_wake", False)),
        }
    )

    with keyboard.Listener(on_press=_on_press, on_release=_on_release) as listener:
        while not stop_event.is_set():
            time.sleep(0.1)
        listener.stop()

    if voice_wake_service is not None:
        voice_wake_service.stop()

    _emit({"event": "stopped"})


def _truncate_text(text: str, *, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."

def _merge_stream_text(prev: str, current: str) -> str:
    """合并流式 ASR 文本：若 current 以 prev 开头则直接返回 current，否则拼接。"""
    if not prev:
        return current
    if current.startswith(prev):
        return current
    if prev.endswith(current):
        return prev
    return prev + current


if __name__ == "__main__":
    main()
