from __future__ import annotations

from enum import Enum


class SettingEffect(str, Enum):
    IMMEDIATE = "immediate"
    NEXT_SESSION = "next_session"
    RESTART_REQUIRED = "restart_required"


_SETTING_EFFECTS: dict[str, SettingEffect] = {
    "auto_hard_enter": SettingEffect.IMMEDIATE,
    "refine_preset": SettingEffect.NEXT_SESSION,
    "sound_on_path": SettingEffect.IMMEDIATE,
    "sound_off_path": SettingEffect.IMMEDIATE,
    "enable_text_refine": SettingEffect.RESTART_REQUIRED,
    "enable_voice_wake": SettingEffect.RESTART_REQUIRED,
    "enable_remote_paste": SettingEffect.RESTART_REQUIRED,
    "remote_paste_host": SettingEffect.RESTART_REQUIRED,
    "remote_paste_port": SettingEffect.RESTART_REQUIRED,
    "remote_paste_timeout_s": SettingEffect.RESTART_REQUIRED,
    "remote_paste_mode": SettingEffect.RESTART_REQUIRED,
    "remote_paste_sync_wait_s": SettingEffect.RESTART_REQUIRED,
    "remote_paste_follow_deskflow_active_screen": SettingEffect.RESTART_REQUIRED,
    "deskflow_active_screen_path": SettingEffect.RESTART_REQUIRED,
    "deskflow_log_path": SettingEffect.RESTART_REQUIRED,
    "remote_paste_screen_name": SettingEffect.RESTART_REQUIRED,
    "hotkey": SettingEffect.RESTART_REQUIRED,
    "stop_hotkey": SettingEffect.RESTART_REQUIRED,
    "toggle_hotkey": SettingEffect.RESTART_REQUIRED,
    "trigger_mode": SettingEffect.RESTART_REQUIRED,
    "cooldown_ms": SettingEffect.RESTART_REQUIRED,
    "notify_backend": SettingEffect.RESTART_REQUIRED,
    "duration": SettingEffect.RESTART_REQUIRED,
    "sample_rate": SettingEffect.RESTART_REQUIRED,
    "channels": SettingEffect.RESTART_REQUIRED,
    "input_device": SettingEffect.RESTART_REQUIRED,
    "record_format": SettingEffect.RESTART_REQUIRED,
    "record_backend": SettingEffect.RESTART_REQUIRED,
    "commit_backend": SettingEffect.RESTART_REQUIRED,
    "asr_provider": SettingEffect.RESTART_REQUIRED,
    "qwen_model": SettingEffect.RESTART_REQUIRED,
    "qwen_language": SettingEffect.RESTART_REQUIRED,
    "qwen_max_new_tokens": SettingEffect.RESTART_REQUIRED,
    "asr_context_preset": SettingEffect.RESTART_REQUIRED,
    "asr_context": SettingEffect.RESTART_REQUIRED,
    "asr_endpoint": SettingEffect.RESTART_REQUIRED,
    "asr_api_key": SettingEffect.RESTART_REQUIRED,
    "asr_timeout_s": SettingEffect.RESTART_REQUIRED,
    "device": SettingEffect.RESTART_REQUIRED,
    "refine_provider": SettingEffect.RESTART_REQUIRED,
    "refine_model": SettingEffect.RESTART_REQUIRED,
    "refine_device": SettingEffect.RESTART_REQUIRED,
    "refine_n_gpu_layers": SettingEffect.RESTART_REQUIRED,
    "refine_max_tokens": SettingEffect.RESTART_REQUIRED,
    "enable_thinking": SettingEffect.RESTART_REQUIRED,
    "refine_api_base": SettingEffect.RESTART_REQUIRED,
    "refine_api_key": SettingEffect.RESTART_REQUIRED,
    "refine_api_model": SettingEffect.RESTART_REQUIRED,
    "warmup": SettingEffect.RESTART_REQUIRED,
    "debug_diagnostics": SettingEffect.RESTART_REQUIRED,
    "wake_prefix": SettingEffect.RESTART_REQUIRED,
    "wake_name": SettingEffect.RESTART_REQUIRED,
    "wake_cooldown_s": SettingEffect.RESTART_REQUIRED,
    "wake_auto_stop_silence_s": SettingEffect.RESTART_REQUIRED,
    "wake_min_speech_s": SettingEffect.RESTART_REQUIRED,
    "wake_use_webrtcvad": SettingEffect.RESTART_REQUIRED,
    "wake_vad_aggressiveness": SettingEffect.RESTART_REQUIRED,
    "wake_vad_frame_ms": SettingEffect.RESTART_REQUIRED,
    "wake_no_speech_timeout_s": SettingEffect.RESTART_REQUIRED,
    "wake_speech_confirm_s": SettingEffect.RESTART_REQUIRED,
    "wake_stats": SettingEffect.RESTART_REQUIRED,
    "wake_pre_vad": SettingEffect.RESTART_REQUIRED,
    "wake_pre_vad_aggressiveness": SettingEffect.RESTART_REQUIRED,
    "wake_pre_vad_frame_ms": SettingEffect.RESTART_REQUIRED,
    "wake_pre_vad_enter_frames": SettingEffect.RESTART_REQUIRED,
    "wake_pre_vad_hangover_ms": SettingEffect.RESTART_REQUIRED,
    "wake_pre_roll_ms": SettingEffect.RESTART_REQUIRED,
    "wake_decode_budget_per_cycle": SettingEffect.RESTART_REQUIRED,
    "wake_decode_budget_per_sec": SettingEffect.RESTART_REQUIRED,
    "wake_auto_name_variants": SettingEffect.RESTART_REQUIRED,
    "wake_auto_prefix_variants": SettingEffect.RESTART_REQUIRED,
    "wake_allow_name_only": SettingEffect.RESTART_REQUIRED,
    "wake_use_semantic_gate": SettingEffect.RESTART_REQUIRED,
    "wake_semantic_probe_interval_s": SettingEffect.RESTART_REQUIRED,
    "wake_semantic_window_s": SettingEffect.RESTART_REQUIRED,
    "wake_semantic_end_silence_s": SettingEffect.RESTART_REQUIRED,
    "wake_semantic_min_chars": SettingEffect.RESTART_REQUIRED,
    "wake_semantic_timeout_ms": SettingEffect.RESTART_REQUIRED,
    "wake_owner_verify": SettingEffect.RESTART_REQUIRED,
    "wake_owner_sample": SettingEffect.RESTART_REQUIRED,
    "wake_owner_profile": SettingEffect.RESTART_REQUIRED,
    "wake_owner_threshold": SettingEffect.RESTART_REQUIRED,
    "wake_owner_window_s": SettingEffect.RESTART_REQUIRED,
    "wake_owner_silence_extend_s": SettingEffect.RESTART_REQUIRED,
    "wake_encoder": SettingEffect.RESTART_REQUIRED,
    "wake_decoder": SettingEffect.RESTART_REQUIRED,
    "wake_joiner": SettingEffect.RESTART_REQUIRED,
    "wake_tokens": SettingEffect.RESTART_REQUIRED,
    "wake_keywords_file": SettingEffect.RESTART_REQUIRED,
    "wake_tokens_type": SettingEffect.RESTART_REQUIRED,
    "wake_provider": SettingEffect.RESTART_REQUIRED,
    "wake_num_threads": SettingEffect.RESTART_REQUIRED,
    "wake_sample_rate": SettingEffect.RESTART_REQUIRED,
    "wake_keyword_score": SettingEffect.RESTART_REQUIRED,
    "wake_keyword_threshold": SettingEffect.RESTART_REQUIRED,
}


def setting_effect_for_key(key: str) -> SettingEffect:
    return _SETTING_EFFECTS.get(str(key), SettingEffect.RESTART_REQUIRED)


def combined_setting_effect(keys: list[str] | set[str] | tuple[str, ...]) -> SettingEffect:
    effect = SettingEffect.IMMEDIATE
    for key in keys:
        current = setting_effect_for_key(str(key))
        if current is SettingEffect.RESTART_REQUIRED:
            return current
        if current is SettingEffect.NEXT_SESSION:
            effect = current
    return effect


def effect_label(effect: SettingEffect) -> str:
    if effect is SettingEffect.IMMEDIATE:
        return "立即生效"
    if effect is SettingEffect.NEXT_SESSION:
        return "下次录音生效"
    return "重启后端生效"


def effect_status_message(effect: SettingEffect, *, restarted: bool) -> str:
    if restarted:
        return "已保存并重启后端，设置已应用"
    if effect is SettingEffect.IMMEDIATE:
        return "已保存，设置立即生效"
    if effect is SettingEffect.NEXT_SESSION:
        return "已保存，设置会在下次录音时生效"
    return "已保存到配置文件；需要重启后端后生效"
