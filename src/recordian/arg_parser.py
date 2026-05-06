"""Argument parsing and configuration for Recordian hotkey dictation.

Provides :func:`build_parser`, :func:`parse_args_with_config`,
:func:`save_runtime_config`, and hotkey-spec parsing helpers.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from recordian.config import ConfigManager
from recordian.refine_capture import DEFAULT_REFINE_CAPTURE_PATH
from recordian.runtime_config import (
    apply_namespace_runtime_normalization,
    normalize_runtime_config,
)

from .audio_feedback import default_sound_off_path, default_sound_on_path
from .linux_dictate import add_dictate_args
from .postprocess_pipeline import _coerce_bool
from .voice_wake import (
    DEFAULT_WAKE_KEYWORD_THRESHOLD,
    DEFAULT_WAKE_NUM_THREADS,
    make_wake_runtime_config,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"

_DEFAULT_WAKE_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
_DEFAULT_WAKE_ENCODER = _DEFAULT_WAKE_MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEFAULT_WAKE_DECODER = _DEFAULT_WAKE_MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEFAULT_WAKE_JOINER = _DEFAULT_WAKE_MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DEFAULT_WAKE_TOKENS = _DEFAULT_WAKE_MODEL_DIR / "tokens.txt"
_DEFAULT_SOUND_ON = default_sound_on_path()
_DEFAULT_SOUND_OFF = default_sound_off_path()


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------
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
        "--capture-refine-samples",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Append pass1/pass2 text pairs into a JSONL file for refinement evaluation",
    )
    parser.add_argument(
        "--capture-refine-samples-path",
        default=DEFAULT_REFINE_CAPTURE_PATH,
        help="Output path for captured refinement samples (JSONL)",
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


# ---------------------------------------------------------------------------
# _parse_args_with_config
# ---------------------------------------------------------------------------
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
        args.wake_semantic_end_silence_s = 1.5
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


# ---------------------------------------------------------------------------
# _save_runtime_config
# ---------------------------------------------------------------------------
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
        "remote_paste_mode": getattr(args, "remote_paste_mode", "direct"),
        "remote_paste_sync_wait_s": getattr(args, "remote_paste_sync_wait_s", 0.35),
        "remote_paste_follow_deskflow_active_screen": bool(
            getattr(args, "remote_paste_follow_deskflow_active_screen", False)
        ),
        "deskflow_active_screen_path": getattr(
            args,
            "deskflow_active_screen_path",
            "~/.local/state/deskflow/active_screen.json",
        ),
        "deskflow_log_path": getattr(args, "deskflow_log_path", ""),
        "remote_paste_screen_name": getattr(args, "remote_paste_screen_name", ""),
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
        "capture_refine_samples": getattr(args, "capture_refine_samples", False),
        "capture_refine_samples_path": getattr(args, "capture_refine_samples_path", DEFAULT_REFINE_CAPTURE_PATH),
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


# ---------------------------------------------------------------------------
# parse_hotkey_spec
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# _expand_key_name
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# _key_to_names
# ---------------------------------------------------------------------------
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
