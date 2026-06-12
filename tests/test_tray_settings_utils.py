from __future__ import annotations

from recordian.tray_settings_utils import (
    HIDDEN_SETTINGS,
    KEY_LABEL_MAP,
    validate_settings_dict,
)
from recordian.voice_wake import DEFAULT_WAKE_KEYWORD_THRESHOLD, DEFAULT_WAKE_NUM_THREADS


class TestValidateSettingsDict:
    def test_missing_keys_get_defaults(self) -> None:
        defaults = {"auto_hard_enter": False, "wake_vad_aggressiveness": 2}
        result = validate_settings_dict({}, defaults=defaults)
        assert result["auto_hard_enter"] is False
        assert result["wake_vad_aggressiveness"] == 2

    def test_invalid_int_clamped_to_default(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_vad_aggressiveness": "bad"}, defaults=defaults)
        assert result["wake_vad_aggressiveness"] == 2

    def test_int_outside_allowed_set_reset(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_vad_aggressiveness": 99}, defaults=defaults)
        assert result["wake_vad_aggressiveness"] == 2

    def test_int_boundary_values(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_vad_aggressiveness": 0}, defaults=defaults)
        assert result["wake_vad_aggressiveness"] == 0
        result = validate_settings_dict({"wake_vad_aggressiveness": 3}, defaults=defaults)
        assert result["wake_vad_aggressiveness"] == 3

    def test_float_clamped_min(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_no_speech_timeout_s": -5.0}, defaults=defaults)
        assert result["wake_no_speech_timeout_s"] == 0.0

    def test_float_range_clamp(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_owner_threshold": 1.5}, defaults=defaults)
        assert result["wake_owner_threshold"] == 0.99
        result = validate_settings_dict({"wake_owner_threshold": -0.1}, defaults=defaults)
        assert result["wake_owner_threshold"] == 0.0

    def test_bool_coercion(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"auto_hard_enter": 1}, defaults=defaults)
        assert result["auto_hard_enter"] is True
        result = validate_settings_dict({"auto_hard_enter": 0}, defaults=defaults)
        assert result["auto_hard_enter"] is False

    def test_str_fallback(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_owner_profile": "   "}, defaults=defaults)
        assert result["wake_owner_profile"] == "~/.config/recordian/owner_voice_profile.json"

    def test_csv_list_from_string(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_prefix": "嗨, 嘿 , 你好"}, defaults=defaults)
        assert result["wake_prefix"] == ["嗨", "嘿", "你好"]

    def test_csv_list_from_list(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_prefix": ["嗨", "嘿"]}, defaults=defaults)
        assert result["wake_prefix"] == ["嗨", "嘿"]

    def test_csv_list_empty_fallback(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({"wake_prefix": ""}, defaults=defaults)
        assert result["wake_prefix"] == ["嗨", "嘿"]

    def test_wake_num_threads_default(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({}, defaults=defaults)
        assert result["wake_num_threads"] == DEFAULT_WAKE_NUM_THREADS

    def test_wake_keyword_threshold_default(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({}, defaults=defaults)
        assert result["wake_keyword_threshold"] == DEFAULT_WAKE_KEYWORD_THRESHOLD

    def test_all_wake_fields_present(self) -> None:
        defaults: dict[str, object] = {}
        result = validate_settings_dict({}, defaults=defaults)
        assert isinstance(result["wake_use_webrtcvad"], bool)
        assert isinstance(result["wake_vad_aggressiveness"], int)
        assert isinstance(result["wake_no_speech_timeout_s"], float)
        assert isinstance(result["wake_prefix"], list)
        assert isinstance(result["wake_name"], list)


class TestHiddenSettings:
    def test_exactly_six_items(self) -> None:
        assert len(HIDDEN_SETTINGS) == 6

    def test_contains_expected_keys(self) -> None:
        expected = {
            "wake_use_semantic_gate",
            "wake_semantic_probe_interval_s",
            "wake_semantic_window_s",
            "wake_semantic_end_silence_s",
            "wake_semantic_min_chars",
            "wake_semantic_timeout_ms",
        }
        assert HIDDEN_SETTINGS == expected


class TestKeyLabelMap:
    def test_common_keys_present(self) -> None:
        common = [
            "hotkey",
            "stop_hotkey",
            "toggle_hotkey",
            "trigger_mode",
            "enable_text_refine",
            "refine_provider",
            "refine_preset",
            "enable_voice_wake",
            "auto_hard_enter",
            "enable_streaming_commit",
            "commit_backend",
            "asr_provider",
            "wake_prefix",
            "wake_name",
            "sound_on_path",
            "sound_off_path",
            "record_backend",
            "record_format",
            "notify_backend",
            "warmup",
            "enable_remote_paste",
        ]
        for key in common:
            assert key in KEY_LABEL_MAP, f"missing label for {key}"

    def test_wake_keys_present(self) -> None:
        wake_keys = [
            "wake_use_webrtcvad",
            "wake_vad_aggressiveness",
            "wake_vad_frame_ms",
            "wake_no_speech_timeout_s",
            "wake_auto_stop_silence_s",
            "wake_min_speech_s",
            "wake_speech_confirm_s",
            "wake_stats",
            "wake_pre_vad",
            "wake_pre_vad_aggressiveness",
            "wake_pre_vad_frame_ms",
            "wake_pre_vad_enter_frames",
            "wake_pre_vad_hangover_ms",
            "wake_pre_roll_ms",
            "wake_decode_budget_per_cycle",
            "wake_decode_budget_per_sec",
            "wake_auto_name_variants",
            "wake_auto_prefix_variants",
            "wake_allow_name_only",
            "wake_owner_verify",
            "wake_owner_profile",
            "wake_owner_sample",
            "wake_owner_threshold",
            "wake_owner_window_s",
            "wake_owner_silence_extend_s",
            "wake_num_threads",
            "wake_keyword_threshold",
            "wake_cooldown_s",
            "wake_beep_path",
        ]
        for key in wake_keys:
            assert key in KEY_LABEL_MAP, f"missing label for {key}"

    def test_hidden_settings_have_labels(self) -> None:
        for key in HIDDEN_SETTINGS:
            assert key in KEY_LABEL_MAP, f"missing label for hidden setting {key}"
