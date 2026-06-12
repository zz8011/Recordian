from __future__ import annotations

from typing import Any

from recordian.voice_wake import DEFAULT_WAKE_KEYWORD_THRESHOLD, DEFAULT_WAKE_NUM_THREADS

HIDDEN_SETTINGS: frozenset[str] = frozenset()

KEY_LABEL_MAP: dict[str, str] = {
    "hotkey": "触发热键",
    "stop_hotkey": "停止热键",
    "toggle_hotkey": "切换热键",
    "trigger_mode": "触发模式",
    "enable_text_refine": "文本精炼",
    "refine_provider": "精炼Provider",
    "refine_preset": "精炼预设",
    "enable_voice_wake": "语音唤醒",
    "auto_hard_enter": "自动硬回车",
    "enable_streaming_commit": "流式上屏",
    "commit_backend": "上屏后端",
    "asr_provider": "ASR Provider",
    "wake_prefix": "唤醒前缀",
    "wake_name": "唤醒名字",
    "sound_on_path": "开始音效",
    "sound_off_path": "结束音效",
    "record_backend": "录音后端",
    "record_format": "录音格式",
    "notify_backend": "通知后端",
    "warmup": "启动预热",
    "enable_remote_paste": "远程粘贴",
    "asr_endpoint": "ASR 接口",
    "asr_api_key": "ASR API Key",
    "qwen_model": "ASR 模型",
    "asr_language": "ASR 语言",
    "asr_context": "常用词",
    "auto_lexicon_db": "自动词库",
    "refine_model": "精炼模型",
    "refine_api_key": "精炼 API Key",
    "refine_api_base": "精炼 API Base",
    "refine_temperature": "精炼温度",
    "refine_max_tokens": "精炼最大Token",
    "refine_timeout": "精炼超时",
    "enable_thinking": "启用思考",
    "wake_use_webrtcvad": "WebRTC VAD",
    "wake_vad_aggressiveness": "VAD 激进程度",
    "wake_vad_frame_ms": "VAD 帧长",
    "wake_no_speech_timeout_s": "无语音超时",
    "wake_auto_stop_silence_s": "自动停止静音",
    "wake_min_speech_s": "最小语音时长",
    "wake_speech_confirm_s": "语音确认时长",
    "wake_stats": "唤醒统计",
    "wake_pre_vad": "预VAD",
    "wake_pre_vad_aggressiveness": "预VAD激进程度",
    "wake_pre_vad_frame_ms": "预VAD帧长",
    "wake_pre_vad_enter_frames": "预VAD进入帧数",
    "wake_pre_vad_hangover_ms": "预VAD hangover",
    "wake_pre_roll_ms": "预滚动时长",
    "wake_decode_budget_per_cycle": "解码预算/周期",
    "wake_decode_budget_per_sec": "解码预算/秒",
    "wake_auto_name_variants": "自动名字变体",
    "wake_auto_prefix_variants": "自动前缀变体",
    "wake_allow_name_only": "允许仅名字",
    "wake_owner_verify": "声纹验证",
    "wake_owner_profile": "声纹档案",
    "wake_owner_sample": "声纹样本",
    "wake_owner_threshold": "声纹阈值",
    "wake_owner_window_s": "声纹窗口",
    "wake_owner_silence_extend_s": "声纹静音延长",
    "wake_num_threads": "唤醒线程数",
    "wake_keyword_threshold": "唤醒关键词阈值",
    "wake_cooldown_s": "唤醒冷却",
    "wake_beep_path": "唤醒提示音",
    "debug_diagnostics": "调试诊断",
    "remote_paste_host": "远程粘贴主机",
    "remote_paste_port": "远程粘贴端口",
    "remote_paste_key": "远程粘贴密钥",
    "remote_paste_timeout": "远程粘贴超时",
    "remote_paste_backend": "远程粘贴后端",
    "remote_paste_auto_connect": "远程粘贴自动连接",
    "remote_paste_retry_interval": "远程粘贴重试间隔",
    "remote_paste_max_retries": "远程粘贴最大重试",
    "remote_paste_buffer_size": "远程粘贴缓冲区",
    "remote_paste_compression": "远程粘贴压缩",
    "remote_paste_encryption": "远程粘贴加密",
    "remote_paste_verify_ssl": "远程粘贴验证SSL",
}


def validate_settings_dict(raw: dict[str, Any], *, defaults: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp all settings from a raw config dict.

    Returns a new dict with every key present (missing keys filled from
    *defaults*), and all numeric / boolean / list fields sanitised exactly
    as ``open_settings()`` did historically.
    """
    current: dict[str, Any] = dict(defaults)
    current.update(raw)

    # --- booleans ----------------------------------------------------------
    current["auto_hard_enter"] = bool(current.get("auto_hard_enter", False))
    current["enable_streaming_commit"] = bool(current.get("enable_streaming_commit", False))
    current["wake_use_webrtcvad"] = bool(current.get("wake_use_webrtcvad", True))
    current["wake_stats"] = bool(current.get("wake_stats", False))
    current["wake_pre_vad"] = bool(current.get("wake_pre_vad", True))
    current["wake_auto_name_variants"] = bool(current.get("wake_auto_name_variants", True))
    current["wake_auto_prefix_variants"] = bool(current.get("wake_auto_prefix_variants", True))
    current["wake_allow_name_only"] = bool(current.get("wake_allow_name_only", True))
    current["wake_owner_verify"] = bool(current.get("wake_owner_verify", False))

    # --- int fields (set membership or min clamp) --------------------------
    def _int_clamp(key: str, default: int, *, allowed: set[int] | None = None, min_val: int | None = None) -> int:
        try:
            val = int(current.get(key, default))
        except Exception:
            val = default
        if allowed is not None and val not in allowed:
            val = default
        if min_val is not None:
            val = max(min_val, val)
        return val

    current["wake_vad_aggressiveness"] = _int_clamp("wake_vad_aggressiveness", 2, allowed={0, 1, 2, 3})
    current["wake_vad_frame_ms"] = _int_clamp("wake_vad_frame_ms", 30, allowed={10, 20, 30})
    current["wake_pre_vad_aggressiveness"] = _int_clamp("wake_pre_vad_aggressiveness", 3, allowed={0, 1, 2, 3})
    current["wake_pre_vad_frame_ms"] = _int_clamp("wake_pre_vad_frame_ms", 30, allowed={10, 20, 30})
    current["wake_pre_vad_enter_frames"] = _int_clamp("wake_pre_vad_enter_frames", 4, min_val=1)
    current["wake_pre_vad_hangover_ms"] = _int_clamp("wake_pre_vad_hangover_ms", 120, min_val=0)
    current["wake_pre_roll_ms"] = _int_clamp("wake_pre_roll_ms", 300, min_val=0)
    current["wake_decode_budget_per_cycle"] = _int_clamp("wake_decode_budget_per_cycle", 1, min_val=1)
    current["wake_num_threads"] = _int_clamp("wake_num_threads", DEFAULT_WAKE_NUM_THREADS, min_val=1)

    # --- float fields (min / range clamp) ----------------------------------
    def _float_clamp(key: str, default: float, *, min_val: float | None = None, max_val: float | None = None) -> float:
        try:
            val = float(current.get(key, default))
        except Exception:
            val = default
        if min_val is not None:
            val = max(min_val, val)
        if max_val is not None:
            val = min(max_val, val)
        return val

    current["wake_no_speech_timeout_s"] = _float_clamp("wake_no_speech_timeout_s", 2.0, min_val=0.0)
    current["wake_auto_stop_silence_s"] = _float_clamp("wake_auto_stop_silence_s", 1.0, min_val=0.0)
    current["wake_min_speech_s"] = _float_clamp("wake_min_speech_s", 0.5, min_val=0.0)
    current["wake_speech_confirm_s"] = _float_clamp("wake_speech_confirm_s", 0.18, min_val=0.0)
    current["wake_decode_budget_per_sec"] = _float_clamp("wake_decode_budget_per_sec", 16.0, min_val=1.0)
    current["wake_owner_threshold"] = _float_clamp("wake_owner_threshold", 0.72, min_val=0.0, max_val=0.99)
    current["wake_owner_window_s"] = _float_clamp("wake_owner_window_s", 1.6, min_val=0.6)
    current["wake_owner_silence_extend_s"] = _float_clamp("wake_owner_silence_extend_s", 0.5, min_val=0.0)
    current["wake_keyword_threshold"] = _float_clamp("wake_keyword_threshold", DEFAULT_WAKE_KEYWORD_THRESHOLD, min_val=0.0)

    # --- str fields (strip with fallback) ----------------------------------
    current["wake_owner_profile"] = str(
        current.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json")
    ).strip() or "~/.config/recordian/owner_voice_profile.json"
    current["wake_owner_sample"] = str(current.get("wake_owner_sample", "")).strip()

    # --- list fields (comma-separated strings) -----------------------------
    def _csv_field(key: str, default: list[str]) -> list[str]:
        raw = current.get(key)
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        raw_str = str(raw).strip() if raw is not None else ""
        if not raw_str:
            return list(default)
        return [item.strip() for item in raw_str.split(",") if item.strip()]

    current["wake_prefix"] = _csv_field("wake_prefix", ["嗨", "嘿"])
    current["wake_name"] = _csv_field("wake_name", ["小二"])

    return current
