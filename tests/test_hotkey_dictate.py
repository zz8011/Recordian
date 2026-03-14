import argparse
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from recordian.hotkey_dictate import (
    _adaptive_vad_threshold,
    _apply_refine_postprocess,
    _apply_target_window,
    _build_refine_prompt_with_protected_terms,
    _cleanup_repeat_lite_text,
    _cleanup_stutter_text,
    _coerce_bool,
    _commit_text,
    _expand_key_name,
    _extract_refine_postprocess_rule,
    _float_to_pcm16le,
    _is_level_speech_frame,
    _merge_stream_text,
    _normalize_final_text,
    _owner_gate_level,
    _pcm16le_to_f32,
    _pick_vad_sample_rate,
    _resample_audio_for_vad,
    _resolve_auto_hard_enter,
    _select_refine_protected_terms,
    _semantic_text_has_content,
    _semantic_text_signal_len,
    _should_auto_stop_semantic_session,
    _should_skip_owner_gated_asr,
    _text_contains_term,
    _update_speech_evidence,
    _vad_frame_bytes,
    build_hotkey_handlers,
    build_ptt_hotkey_handlers,
    parse_hotkey_spec,
)
from recordian.linux_dictate import DictateResult, RecordProcessHandle


def _fake_args() -> argparse.Namespace:
    return argparse.Namespace(cooldown_ms=0)


def test_hotkey_handler_emits_result(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    def _fake_run_dictate_once(args):  # noqa: ANN001
        return DictateResult(
            audio_path="/tmp/a.wav",
            record_backend="arecord",
            duration_s=1.0,
            record_latency_ms=100.0,
            transcribe_latency_ms=200.0,
            text="你好",
            commit={"backend": "none", "committed": False, "detail": "disabled"},
        )

    monkeypatch.setattr("recordian.hotkey_dictate.run_dictate_once", _fake_run_dictate_once)
    run_once, _, _ = build_hotkey_handlers(
        args=_fake_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
    )
    run_once()
    time.sleep(0.05)

    assert events
    assert events[0]["event"] == "result"


def test_hotkey_handler_emits_busy(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    def _slow_run_dictate_once(args):  # noqa: ANN001
        time.sleep(0.1)
        return DictateResult(
            audio_path="/tmp/a.wav",
            record_backend="arecord",
            duration_s=1.0,
            record_latency_ms=100.0,
            transcribe_latency_ms=200.0,
            text="你好",
            commit={"backend": "none", "committed": False, "detail": "disabled"},
        )

    monkeypatch.setattr("recordian.hotkey_dictate.run_dictate_once", _slow_run_dictate_once)
    run_once, _, _ = build_hotkey_handlers(
        args=_fake_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
    )
    run_once()
    run_once()
    time.sleep(0.02)
    assert any(event.get("event") == "busy" for event in events)


def test_hotkey_exit_sets_stop_event() -> None:
    run_once, exit_daemon, stop_event = build_hotkey_handlers(
        args=_fake_args(),
        on_result=lambda payload: None,
        on_error=lambda payload: None,
        on_busy=lambda payload: None,
    )
    assert run_once is not None
    assert not stop_event.is_set()
    exit_daemon()
    assert stop_event.is_set()


def test_parse_hotkey_spec_aliases() -> None:
    keys = parse_hotkey_spec("<control>+<option>+V")
    assert keys == {"ctrl", "alt", "v"}
    assert parse_hotkey_spec("<menu>") == {"menu"}
    assert parse_hotkey_spec("vk:135") == {"vk:135"}
    assert parse_hotkey_spec("135") == {"vk:135"}
    assert parse_hotkey_spec("<ctrl_r>") == {"ctrl_r"}
    assert parse_hotkey_spec("rightctrl") == {"ctrl_r"}


def test_expand_key_name_supports_side_specific_ctrl() -> None:
    assert _expand_key_name("ctrl_r") == {"ctrl_r", "ctrl"}
    assert _expand_key_name("ctrl_l") == {"ctrl_l", "ctrl"}
    assert _expand_key_name("alt_gr") == {"alt_gr", "alt"}
    assert _expand_key_name("application") == {"menu"}


def test_merge_stream_text() -> None:
    assert _merge_stream_text("", "你") == "你"
    assert _merge_stream_text("你", "你好") == "你好"
    assert _merge_stream_text("你好", "好") == "你好"
    assert _merge_stream_text("你好", "世界") == "你好世界"


def test_normalize_final_text_reduces_simple_repeats() -> None:
    assert _normalize_final_text("你好你好") == "你好"
    assert _normalize_final_text("上海天气天气") == "上海天气"
    assert _normalize_final_text("你好世界") == "你好世界"


def test_cleanup_stutter_text_collapses_common_repetitions() -> None:
    assert _cleanup_stutter_text("要要要要把这个功能做出来。") == "要把这个功能做出来。"
    assert _cleanup_stutter_text("这个这个我觉得就是就是可以先试试。") == "这个我觉得就是可以先试试。"
    assert _cleanup_stutter_text("我们我们先处理。") == "我们先处理。"
    assert _cleanup_stutter_text("我 我 我觉得可以。") == "我觉得可以。"
    assert _cleanup_stutter_text("然后呢，同时呢，然后a，然后这个语气词还没有优化。") == "语气词还没有优化。"
    assert _cleanup_stutter_text("嗯，啊，呃，我想继续测试。") == "我想继续测试。"
    assert _cleanup_stutter_text("看看这个问题。") == "看看这个问题。"
    assert _cleanup_stutter_text("范冰冰和李冰冰都来了。") == "范冰冰和李冰冰都来了。"
    assert _cleanup_stutter_text("人人都要努力。") == "人人都要努力。"


def test_cleanup_repeat_lite_text_for_space_separated_languages() -> None:
    assert _cleanup_repeat_lite_text("I I I think this works.") == "I think this works."
    assert _cleanup_repeat_lite_text("the the issue is fixed") == "the issue is fixed"
    assert _cleanup_repeat_lite_text("Tom met Lily.") == "Tom met Lily."


def test_extract_refine_postprocess_rule_from_prompt_template() -> None:
    rule, prompt = _extract_refine_postprocess_rule("@postprocess: zh-stutter-lite\n原文：{text}")
    assert rule == "zh-stutter-lite"
    assert prompt == "原文：{text}"

    rule, prompt = _extract_refine_postprocess_rule("\n@postprocess: repeat-lite\nLine2")
    assert rule == "repeat-lite"
    assert prompt == "Line2"

    rule, prompt = _extract_refine_postprocess_rule("原文：{text}")
    assert rule == "none"
    assert prompt == "原文：{text}"


def test_apply_refine_postprocess_dispatches_by_rule() -> None:
    assert _apply_refine_postprocess("要要要把功能做出来。", rule="zh-stutter-lite") == "要把功能做出来。"
    assert _apply_refine_postprocess("I I agree.", rule="repeat-lite") == "I agree."
    assert _apply_refine_postprocess("范冰冰", rule="none") == "范冰冰"


def test_text_contains_term_supports_ascii_casefold_and_cjk() -> None:
    assert _text_contains_term("Docker setup is ready", "docker")
    assert _text_contains_term("范冰冰和李冰冰", "李冰冰")
    assert not _text_contains_term("hello world", "docker")


def test_select_refine_protected_terms_filters_noise_and_keeps_domain_terms() -> None:
    text = "我们先把 Docker 和 Recordian 的 GitHub 问题处理一下。"
    hotwords = ["我们", "docker", "Recordian", "GitHub", "这个", "不存在词"]
    selected = _select_refine_protected_terms(text, hotwords, max_terms=8)
    assert "docker" in selected
    assert "Recordian" in selected
    assert "GitHub" in selected
    assert "我们" not in selected
    assert "这个" not in selected
    assert "不存在词" not in selected


def test_build_refine_prompt_with_protected_terms_injects_guard() -> None:
    base = "请整理文本：{text}"
    wrapped = _build_refine_prompt_with_protected_terms(base, ["Docker", "Recordian"])
    assert wrapped is not None
    assert "Docker、Recordian" in wrapped
    assert wrapped.endswith(base)
    assert _build_refine_prompt_with_protected_terms(base, []) == base
    assert _build_refine_prompt_with_protected_terms(None, ["Docker"]) is None


def test_commit_text_handles_generic_exception() -> None:
    class _BrokenCommitter:
        backend_name = "broken"

        def commit(self, text: str):  # noqa: ANN001
            raise RuntimeError("boom")

    payload = _commit_text(_BrokenCommitter(), "你好")
    assert payload["backend"] == "broken"
    assert payload["committed"] is False
    assert "boom" in str(payload["detail"])


def test_commit_text_appends_hard_enter_detail_when_enabled(monkeypatch) -> None:
    class _OkCommitter:
        backend_name = "xdotool"

        def commit(self, text: str):  # noqa: ANN001
            class _R:
                backend = "xdotool"
                committed = True
                detail = "typed"

            return _R()

    class _EnterR:
        committed = True
        detail = "hard_enter_sent"

    monkeypatch.setattr("recordian.hotkey_dictate.send_hard_enter", lambda committer: _EnterR())
    payload = _commit_text(_OkCommitter(), "你好", auto_hard_enter=True)
    assert payload["committed"] is True
    assert "typed" in str(payload["detail"])
    assert "hard_enter_sent" in str(payload["detail"])


def test_resolve_auto_hard_enter_reads_config(tmp_path: Path) -> None:
    cfg = tmp_path / "hotkey.json"
    cfg.write_text(json.dumps({"auto_hard_enter": "true"}, ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(auto_hard_enter=False, config_path=str(cfg))
    assert _resolve_auto_hard_enter(args) is True


def test_coerce_bool() -> None:
    assert _coerce_bool(True) is True
    assert _coerce_bool("1") is True
    assert _coerce_bool("off", default=True) is False
    assert _coerce_bool("unknown", default=True) is True


def test_adaptive_vad_threshold_clamped_range() -> None:
    base = 0.045
    assert abs(_adaptive_vad_threshold(base, 0.0) - 0.018) < 1e-6
    assert 0.018 <= _adaptive_vad_threshold(base, 0.01) <= base
    assert abs(_adaptive_vad_threshold(base, 0.05) - base) < 1e-6


def test_vad_helpers_choose_sample_rate_and_frame_size() -> None:
    assert _pick_vad_sample_rate(48000) == 48000
    assert _pick_vad_sample_rate(44100) == 16000
    assert _vad_frame_bytes(16000, 30) == 960


def test_float_to_pcm16le_and_resample_roundtrip() -> None:
    src = [0.0, 0.5, -0.5, 1.2, -1.2]
    pcm = _float_to_pcm16le(src)
    decoded = _pcm16le_to_f32(pcm, channels=1)
    assert len(decoded) == len(src)
    assert 0.49 < decoded[1] < 0.51
    assert -0.51 < decoded[2] < -0.49
    assert decoded[3] <= 1.0
    assert decoded[4] >= -1.0

    resampled = _resample_audio_for_vad([0.0, 0.2, 0.4, 0.6], src_rate=8000, dst_rate=16000)
    assert len(resampled) >= 7


def test_pcm16le_to_f32_mono_and_stereo() -> None:
    mono = _pcm16le_to_f32(b"\x00\x40\x00\xc0", channels=1)
    assert len(mono) == 2
    assert 0.49 < mono[0] < 0.51
    assert -0.51 < mono[1] < -0.49

    stereo = _pcm16le_to_f32(b"\x00\x40\x00\x40\x00\xc0\x00\xc0", channels=2)
    assert len(stereo) == 2
    assert 0.49 < stereo[0] < 0.51
    assert -0.51 < stereo[1] < -0.49


def test_default_config_path_uses_home_dir() -> None:
    from recordian.hotkey_dictate import DEFAULT_CONFIG_PATH
    assert DEFAULT_CONFIG_PATH.startswith("~"), f"应以 ~ 开头，实际: {DEFAULT_CONFIG_PATH}"


def test_parse_hotkey_spec_empty_vk_prefix() -> None:
    from recordian.hotkey_dictate import parse_hotkey_spec
    result = parse_hotkey_spec("vk:")
    assert "vk:" not in result, f"空 vk: 不应出现在结果中，实际: {result}"


def test_build_parser_accepts_llamacpp_refine_provider() -> None:
    from recordian.hotkey_dictate import build_parser

    parser = build_parser()
    args = parser.parse_args(["--refine-provider", "llamacpp"])
    assert args.refine_provider == "llamacpp"


def test_build_parser_accepts_voice_wake_options() -> None:
    from recordian.hotkey_dictate import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "--enable-voice-wake",
            "--wake-prefix",
            "嘿",
            "--wake-name",
            "小二",
            "--wake-cooldown-s",
            "3",
            "--wake-vad-aggressiveness",
            "3",
            "--wake-vad-frame-ms",
            "20",
            "--wake-no-speech-timeout-s",
            "2.5",
            "--wake-speech-confirm-s",
            "0.24",
            "--wake-use-semantic-gate",
            "--wake-auto-prefix-variants",
            "--wake-allow-name-only",
            "--wake-owner-verify",
            "--wake-owner-profile",
            "/tmp/owner-profile.json",
            "--wake-owner-sample",
            "/tmp/owner-sample.wav",
            "--wake-owner-threshold",
            "0.77",
            "--wake-owner-window-s",
            "1.9",
            "--wake-semantic-probe-interval-s",
            "0.6",
            "--wake-semantic-window-s",
            "1.4",
            "--wake-semantic-end-silence-s",
            "1.1",
            "--wake-semantic-min-chars",
            "2",
            "--wake-semantic-timeout-ms",
            "1600",
            "--auto-hard-enter",
            "--sound-on-path",
            "/tmp/on.mp3",
            "--sound-off-path",
            "/tmp/off.mp3",
            "--enable-auto-lexicon",
            "--auto-lexicon-db",
            "/tmp/auto_lexicon.db",
            "--auto-lexicon-max-hotwords",
            "88",
            "--auto-lexicon-min-accepts",
            "3",
            "--auto-lexicon-max-terms",
            "6666",
        ]
    )
    assert args.enable_voice_wake is True
    assert "嘿" in args.wake_prefix
    assert "小二" in args.wake_name
    assert args.wake_vad_aggressiveness == 3
    assert args.wake_vad_frame_ms == 20
    assert args.wake_no_speech_timeout_s == 2.5
    assert args.wake_speech_confirm_s == 0.24
    assert args.wake_use_semantic_gate is True
    assert args.wake_auto_prefix_variants is True
    assert args.wake_allow_name_only is True
    assert args.wake_owner_verify is True
    assert args.wake_owner_profile == "/tmp/owner-profile.json"
    assert args.wake_owner_sample == "/tmp/owner-sample.wav"
    assert args.wake_owner_threshold == 0.77
    assert args.wake_owner_window_s == 1.9
    assert args.wake_semantic_probe_interval_s == 0.6
    assert args.wake_semantic_window_s == 1.4
    assert args.wake_semantic_end_silence_s == 1.1
    assert args.wake_semantic_min_chars == 2
    assert args.wake_semantic_timeout_ms == 1600
    assert args.auto_hard_enter is True
    assert args.sound_on_path == "/tmp/on.mp3"
    assert args.sound_off_path == "/tmp/off.mp3"
    assert args.enable_auto_lexicon is True
    assert args.auto_lexicon_db == "/tmp/auto_lexicon.db"
    assert args.auto_lexicon_max_hotwords == 88
    assert args.auto_lexicon_min_accepts == 3
    assert args.auto_lexicon_max_terms == 6666


def test_build_parser_uses_cpu_friendly_wake_defaults() -> None:
    from recordian.hotkey_dictate import build_parser

    parser = build_parser()
    args = parser.parse_args([])

    assert args.wake_num_threads == 1
    assert args.wake_keyword_threshold == 0.12


def test_parse_args_with_config_normalizes_legacy_values(tmp_path: Path, monkeypatch) -> None:
    from recordian.hotkey_dictate import _parse_args_with_config, build_parser

    cfg = tmp_path / "hotkey.json"
    cfg.write_text(
        json.dumps(
            {
                "refine_enable_thinking": True,
                "refine_provider": "llama.cpp",
                "refine_model_llamacpp": "/tmp/qwen.gguf",
                "record_backend": "ffmpeg",
                "record_format": "mp3",
                "commit_backend": "pynput",
                "auto_hard_enter": True,
                "wake_vad_aggressiveness": 9,
                "wake_vad_frame_ms": 25,
                "wake_no_speech_timeout_s": -3,
                "wake_speech_confirm_s": -1.0,
                "wake_use_semantic_gate": "true",
                "wake_auto_prefix_variants": "false",
                "wake_allow_name_only": "0",
                "wake_owner_verify": "true",
                "wake_owner_profile": "~/owner_profile.json",
                "wake_owner_sample": "~/owner_sample.wav",
                "wake_owner_threshold": 9.9,
                "wake_owner_window_s": 0.1,
                "wake_semantic_probe_interval_s": -0.1,
                "wake_semantic_window_s": 0.1,
                "wake_semantic_end_silence_s": 0.0,
                "wake_semantic_min_chars": 0,
                "wake_semantic_timeout_ms": 100,
                "enable_auto_lexicon": "false",
                "auto_lexicon_max_hotwords": -10,
                "auto_lexicon_min_accepts": 0,
                "auto_lexicon_max_terms": -20,
                "auto_lexicon_db": "~/tmp/recordian-lexicon.db",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("sys.argv", ["recordian-hotkey-dictate", "--config-path", str(cfg)])
    args = _parse_args_with_config(build_parser())

    assert args.enable_thinking is True
    assert args.refine_provider == "llamacpp"
    assert args.refine_model == "/tmp/qwen.gguf"
    assert args.record_backend == "ffmpeg-pulse"
    assert args.record_format == "ogg"
    assert args.commit_backend == "auto"
    assert args.auto_hard_enter is True
    assert args.wake_vad_aggressiveness == 2
    assert args.wake_vad_frame_ms == 30
    assert args.wake_no_speech_timeout_s == 0.0
    assert args.wake_speech_confirm_s == 0.0
    assert args.wake_use_semantic_gate is True
    assert args.wake_auto_prefix_variants is False
    assert args.wake_allow_name_only is False
    assert args.wake_owner_verify is True
    assert args.wake_owner_profile.endswith("owner_profile.json")
    assert args.wake_owner_sample.endswith("owner_sample.wav")
    assert args.wake_owner_threshold == 0.99
    assert args.wake_owner_window_s == 0.6
    assert args.wake_semantic_probe_interval_s == 0.1
    assert args.wake_semantic_window_s == 0.4
    assert args.wake_semantic_end_silence_s == 0.2
    assert args.wake_semantic_min_chars == 1
    assert args.wake_semantic_timeout_ms == 200
    assert args.enable_auto_lexicon is False
    assert args.auto_lexicon_max_hotwords == 0
    assert args.auto_lexicon_min_accepts == 1
    assert args.auto_lexicon_max_terms == 100
    assert args.auto_lexicon_db.endswith("recordian-lexicon.db")


def test_parse_args_accepts_auto_fallback_commit_backend(monkeypatch) -> None:
    from recordian.hotkey_dictate import _parse_args_with_config, build_parser

    monkeypatch.setattr("sys.argv", ["recordian-hotkey-dictate", "--commit-backend", "auto-fallback"])
    args = _parse_args_with_config(build_parser())

    assert args.commit_backend == "auto-fallback"


def test_parse_args_with_config_preserves_auto_fallback_commit_backend(tmp_path: Path, monkeypatch) -> None:
    from recordian.hotkey_dictate import _parse_args_with_config, build_parser

    cfg = tmp_path / "hotkey.json"
    cfg.write_text(json.dumps({"commit_backend": "auto-fallback"}), encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["recordian-hotkey-dictate", "--config-path", str(cfg)])
    args = _parse_args_with_config(build_parser())

    assert args.commit_backend == "auto-fallback"


def test_level_speech_frame_requires_signal_and_energy() -> None:
    assert _is_level_speech_frame(level=0.18, rms=0.01, noise_floor=0.0015) is True
    assert _is_level_speech_frame(level=0.18, rms=0.001, noise_floor=0.0015) is False
    assert _is_level_speech_frame(level=0.02, rms=0.02, noise_floor=0.0015) is False


def test_update_speech_evidence_smooths_transient_spikes() -> None:
    score = 0.0
    confirmed = False
    for _ in range(6):
        score, confirmed = _update_speech_evidence(
            score,
            speech_detected_raw=True,
            frame_duration_s=0.064,
            confirm_s=0.18,
        )
    assert confirmed is True
    assert score > 0.18

    score, confirmed = _update_speech_evidence(
        score,
        speech_detected_raw=False,
        frame_duration_s=0.064,
        confirm_s=0.18,
    )
    assert confirmed is True

    for _ in range(8):
        score, confirmed = _update_speech_evidence(
            score,
            speech_detected_raw=False,
            frame_duration_s=0.064,
            confirm_s=0.18,
        )
    assert confirmed is False


def test_owner_gate_level_hides_non_owner_activity() -> None:
    assert _owner_gate_level(0.66, owner_filter_enabled=False, owner_active=False) == 0.66
    assert _owner_gate_level(0.66, owner_filter_enabled=True, owner_active=True) == 0.66
    assert _owner_gate_level(0.66, owner_filter_enabled=True, owner_active=False) == 0.0
    assert _owner_gate_level(-1.0, owner_filter_enabled=False, owner_active=True) == 0.0
    assert _owner_gate_level(2.0, owner_filter_enabled=True, owner_active=True) == 1.0


def test_should_skip_owner_gated_asr_requires_owner_presence() -> None:
    assert _should_skip_owner_gated_asr(owner_filter_enabled=True, owner_seen=False) is True
    assert _should_skip_owner_gated_asr(owner_filter_enabled=True, owner_seen=True) is False
    assert _should_skip_owner_gated_asr(owner_filter_enabled=False, owner_seen=False) is False


def test_semantic_text_has_content_by_effective_chars() -> None:
    assert _semantic_text_signal_len("  ，。  ") == 0
    assert _semantic_text_signal_len("嗯嗯") == 2
    assert _semantic_text_signal_len("abc-12") == 5
    assert _semantic_text_has_content("。。", min_chars=1) is False
    assert _semantic_text_has_content("好", min_chars=1) is True
    assert _semantic_text_has_content("ok", min_chars=3) is False


def test_semantic_no_text_timeout_waits_when_recent_acoustic_speech() -> None:
    reason = _should_auto_stop_semantic_session(
        now_ts=5.0,
        started_ts=0.0,
        last_speech_ts=4.2,
        semantic_has_text=False,
        semantic_last_text_ts=0.0,
        no_speech_timeout_s=2.0,
        min_speech_s=0.5,
        semantic_end_silence_s=0.85,
        acoustic_silence_s=1.0,
    )
    assert reason is None

    reason = _should_auto_stop_semantic_session(
        now_ts=5.0,
        started_ts=0.0,
        last_speech_ts=2.5,
        semantic_has_text=False,
        semantic_last_text_ts=0.0,
        no_speech_timeout_s=2.0,
        min_speech_s=0.5,
        semantic_end_silence_s=0.85,
        acoustic_silence_s=1.0,
    )
    assert reason == "semantic_no_text_timeout"


def test_semantic_silence_requires_both_semantic_and_acoustic_gaps() -> None:
    reason = _should_auto_stop_semantic_session(
        now_ts=4.0,
        started_ts=0.0,
        last_speech_ts=3.5,
        semantic_has_text=True,
        semantic_last_text_ts=2.9,
        no_speech_timeout_s=2.0,
        min_speech_s=0.5,
        semantic_end_silence_s=0.85,
        acoustic_silence_s=1.0,
    )
    assert reason is None

    reason = _should_auto_stop_semantic_session(
        now_ts=4.0,
        started_ts=0.0,
        last_speech_ts=2.8,
        semantic_has_text=True,
        semantic_last_text_ts=2.9,
        no_speech_timeout_s=2.0,
        min_speech_s=0.5,
        semantic_end_silence_s=0.85,
        acoustic_silence_s=1.0,
    )
    assert reason == "semantic_silence"


def test_ptt_and_toggle_concurrent_trigger_no_conflict(monkeypatch) -> None:
    """PTT 和 toggle 并行触发时不应冲突"""
    import time

    from recordian.hotkey_dictate import build_hotkey_handlers
    from recordian.linux_dictate import DictateResult

    events: list[dict[str, object]] = []
    call_count = {"count": 0}

    def _fake_run_dictate_once(args):  # noqa: ANN001
        call_count["count"] += 1
        time.sleep(0.05)  # 模拟录音延迟
        return DictateResult(
            audio_path=f"/tmp/audio_{call_count['count']}.wav",
            record_backend="arecord",
            duration_s=1.0,
            record_latency_ms=100.0,
            transcribe_latency_ms=200.0,
            text=f"文本{call_count['count']}",
            commit={"backend": "none", "committed": False, "detail": "disabled"},
        )

    monkeypatch.setattr("recordian.hotkey_dictate.run_dictate_once", _fake_run_dictate_once)

    args = _fake_args()
    run_once, _, _ = build_hotkey_handlers(
        args=args,
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
    )

    # 快速连续触发两次（模拟 PTT 和 toggle 同时触发）
    run_once()
    run_once()

    time.sleep(0.15)

    # 应该有 1 个 result + 1 个 busy 事件
    assert len(events) == 2
    result_events = [e for e in events if e.get("event") == "result"]
    busy_events = [e for e in events if e.get("event") == "busy"]
    assert len(result_events) == 1, "应该只有一个识别结果"
    assert len(busy_events) == 1, "第二次触发应返回 busy"


def test_toggle_mode_concurrent_start_stop_safe(monkeypatch) -> None:
    """toggle 模式下快速开始-停止不应崩溃"""
    import time

    from recordian.hotkey_dictate import build_hotkey_handlers
    from recordian.linux_dictate import DictateResult

    events: list[dict[str, object]] = []

    def _fake_run_dictate_once(args):  # noqa: ANN001
        time.sleep(0.1)  # 模拟录音
        return DictateResult(
            audio_path="/tmp/audio.wav",
            record_backend="arecord",
            duration_s=1.0,
            record_latency_ms=100.0,
            transcribe_latency_ms=200.0,
            text="测试",
            commit={"backend": "none", "committed": False, "detail": "disabled"},
        )

    monkeypatch.setattr("recordian.hotkey_dictate.run_dictate_once", _fake_run_dictate_once)

    args = _fake_args()
    run_once, _, _ = build_hotkey_handlers(
        args=args,
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
    )

    # 快速开始-停止（模拟用户误触）
    run_once()  # 开始
    time.sleep(0.01)
    run_once()  # 立即停止

    time.sleep(0.15)

    # 不应崩溃，应该有 busy 事件
    assert len(events) >= 1
    # 至少有一个 busy 或 result 事件
    assert any(e.get("event") in ("busy", "result") for e in events)


def test_apply_target_window_applies_window_anchor() -> None:
    class _Committer:
        target_window_id = None

    committer = _Committer()
    state = {
        "target_window_id": 456,
    }
    _apply_target_window(committer, state)
    assert committer.target_window_id == 456


def test_ptt_handlers_survive_refiner_warmup_failure(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class _FakeProvider:
        provider_name = "http-cloud"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
            return SimpleNamespace(text="你好")

    class _FakeCommitter:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail="printed")

    class _BrokenRefiner:
        provider_name = "cloud-llm:test"
        model = "test"
        prompt_template = "原文：{text}"

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            pass

        def refine(self, text: str) -> str:
            raise ConnectionError("refiner down")

    class _PresetManager:
        def load_preset(self, name: str) -> str:
            return "原文：{text}"

    monkeypatch.setattr("recordian.hotkey_dictate.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.hotkey_dictate.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.hotkey_dictate.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.hotkey_dictate.create_provider", lambda args: _FakeProvider())
    monkeypatch.setattr("recordian.providers.CloudLLMRefiner", _BrokenRefiner)
    monkeypatch.setattr("recordian.preset_manager.PresetManager", _PresetManager)

    args = argparse.Namespace(
        cooldown_ms=0,
        record_backend="ffmpeg-pulse",
        commit_backend="stdout",
        enable_auto_lexicon=False,
        debug_diagnostics=False,
        enable_text_refine=True,
        refine_prompt="",
        refine_preset="default",
        refine_provider="cloud",
        refine_api_key="token",
        refine_api_base="http://127.0.0.1:8018/v1",
        refine_api_model="demo",
        refine_max_tokens=128,
        enable_thinking=False,
        warmup=True,
    )

    start_recording, stop_recording, exit_daemon, stop_event = build_ptt_hotkey_handlers(
        args=args,
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )

    assert callable(start_recording)
    assert callable(stop_recording)
    assert callable(exit_daemon)
    assert stop_event.is_set() is False
    assert any(
        event.get("event") == "refiner_warmup" and event.get("status") == "failed"
        for event in events
    )


def test_ptt_handlers_fall_back_to_raw_text_when_refiner_fails(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class _FakeProvider:
        provider_name = "http-cloud"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
            return SimpleNamespace(text="你好")

    class _FakeCommitter:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail="printed")

    class _BrokenRefiner:
        provider_name = "cloud-llm:test"
        model = "test"
        prompt_template = "原文：{text}"

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            pass

        def refine(self, text: str) -> str:
            raise ConnectionError("refiner down")

    class _PresetManager:
        def load_preset(self, name: str) -> str:
            return "原文：{text}"

    class _FakeProcess:
        def poll(self) -> int:
            return 0

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        output_path = kwargs["output_path"]
        output_path.write_bytes(b"")
        return RecordProcessHandle(
            process=_FakeProcess(),
            monitor_stream=io.BytesIO(b""),
            monitor_sample_rate=16000,
            monitor_channels=1,
        )

    monkeypatch.setattr("recordian.hotkey_dictate.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.hotkey_dictate.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.hotkey_dictate.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.hotkey_dictate.create_provider", lambda args: _FakeProvider())
    monkeypatch.setattr("recordian.hotkey_dictate.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.hotkey_dictate.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.hotkey_dictate.stop_record_process", lambda *args, **kwargs: None)
    monkeypatch.setattr("recordian.providers.CloudLLMRefiner", _BrokenRefiner)
    monkeypatch.setattr("recordian.preset_manager.PresetManager", _PresetManager)

    args = argparse.Namespace(
        cooldown_ms=0,
        record_backend="ffmpeg-pulse",
        commit_backend="stdout",
        enable_auto_lexicon=False,
        debug_diagnostics=False,
        enable_text_refine=True,
        refine_prompt="",
        refine_preset="default",
        refine_provider="cloud",
        refine_api_key="token",
        refine_api_base="http://127.0.0.1:8018/v1",
        refine_api_model="demo",
        refine_max_tokens=128,
        enable_thinking=False,
        warmup=False,
        record_format="ogg",
        input_device="default",
        channels=1,
        sample_rate=16000,
        wake_use_semantic_gate=False,
        wake_owner_verify=False,
        hotword=[],
        auto_hard_enter=False,
        enable_streaming_refine=False,
    )

    start_recording, stop_recording, _, _ = build_ptt_hotkey_handlers(
        args=args,
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )

    start_recording("voice_wake")
    time.sleep(0.02)
    stop_recording()
    time.sleep(0.15)

    result_events = [event for event in events if event.get("event") == "result"]
    assert result_events
    assert result_events[-1]["result"]["text"] == "你好"
    assert any(
        "text_refine_failed" in str(event.get("message", ""))
        for event in events
        if event.get("event") == "log"
    )


def _fake_ptt_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "cooldown_ms": 0,
        "record_backend": "ffmpeg-pulse",
        "commit_backend": "stdout",
        "enable_auto_lexicon": False,
        "debug_diagnostics": False,
        "enable_text_refine": False,
        "warmup": False,
        "record_format": "wav",
        "input_device": "default",
        "channels": 1,
        "sample_rate": 16000,
        "wake_use_semantic_gate": False,
        "wake_owner_verify": False,
        "hotword": [],
        "auto_hard_enter": False,
        "enable_streaming_refine": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_ptt_start_recording_returns_false_when_busy(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class _FakeProvider:
        provider_name = "http-cloud"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
            return SimpleNamespace(text="忙时测试")

    class _FakeCommitter:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail="printed")

    class _FakeProcess:
        def poll(self) -> int:
            return 0

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        output_path = kwargs["output_path"]
        output_path.write_bytes(b"")
        return RecordProcessHandle(process=_FakeProcess(), monitor_stream=io.BytesIO(b""))

    monkeypatch.setattr("recordian.hotkey_dictate.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.hotkey_dictate.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.hotkey_dictate.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.hotkey_dictate.create_provider", lambda args: _FakeProvider())
    monkeypatch.setattr("recordian.hotkey_dictate.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.hotkey_dictate.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.hotkey_dictate.stop_record_process", lambda *args, **kwargs: None)

    start_recording, stop_recording, _, _ = build_ptt_hotkey_handlers(
        args=_fake_ptt_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )

    assert start_recording() is True
    assert start_recording() is False
    stop_recording()
    time.sleep(0.1)

    busy_events = [event for event in events if event.get("event") == "busy"]
    assert len(busy_events) == 1


def test_ptt_start_failure_releases_lock_and_recovers(monkeypatch) -> None:
    events: list[dict[str, object]] = []
    start_attempts = {"count": 0}

    class _FakeProvider:
        provider_name = "http-cloud"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
            return SimpleNamespace(text="恢复成功")

    class _FakeCommitter:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail="printed")

    class _FakeProcess:
        def poll(self) -> int:
            return 0

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        start_attempts["count"] += 1
        if start_attempts["count"] == 1:
            raise RuntimeError("recorder failed")
        output_path = kwargs["output_path"]
        output_path.write_bytes(b"")
        return RecordProcessHandle(process=_FakeProcess(), monitor_stream=io.BytesIO(b""))

    monkeypatch.setattr("recordian.hotkey_dictate.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.hotkey_dictate.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.hotkey_dictate.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.hotkey_dictate.create_provider", lambda args: _FakeProvider())
    monkeypatch.setattr("recordian.hotkey_dictate.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.hotkey_dictate.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.hotkey_dictate.stop_record_process", lambda *args, **kwargs: None)

    start_recording, stop_recording, _, _ = build_ptt_hotkey_handlers(
        args=_fake_ptt_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )

    with pytest.raises(RuntimeError, match="recorder failed"):
        start_recording()

    assert start_recording() is True
    stop_recording()
    time.sleep(0.1)

    result_events = [event for event in events if event.get("event") == "result"]
    assert len(result_events) == 1
    assert result_events[0]["result"]["text"] == "恢复成功"


def test_ptt_stop_recording_is_idempotent_under_concurrency(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class _FakeProvider:
        provider_name = "http-cloud"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
            time.sleep(0.05)
            return SimpleNamespace(text="并发停止")

    class _FakeCommitter:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail="printed")

    class _FakeProcess:
        def poll(self) -> int:
            return 0

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        output_path = kwargs["output_path"]
        output_path.write_bytes(b"")
        return RecordProcessHandle(process=_FakeProcess(), monitor_stream=io.BytesIO(b""))

    monkeypatch.setattr("recordian.hotkey_dictate.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.hotkey_dictate.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.hotkey_dictate.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.hotkey_dictate.create_provider", lambda args: _FakeProvider())
    monkeypatch.setattr("recordian.hotkey_dictate.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.hotkey_dictate.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.hotkey_dictate.stop_record_process", lambda *args, **kwargs: None)

    start_recording, stop_recording, _, _ = build_ptt_hotkey_handlers(
        args=_fake_ptt_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )

    assert start_recording() is True

    workers = [threading.Thread(target=stop_recording), threading.Thread(target=stop_recording)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    time.sleep(0.15)

    result_events = [event for event in events if event.get("event") == "result"]
    error_events = [event for event in events if event.get("event") == "error"]
    assert len(result_events) == 1
    assert not error_events


def test_ptt_exit_waits_for_processing_completion(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class _FakeProvider:
        provider_name = "http-cloud"

        def transcribe_file(self, audio_path: Path, hotwords: list[str]) -> SimpleNamespace:  # noqa: ANN001
            time.sleep(0.08)
            return SimpleNamespace(text="退出等待")

    class _FakeCommitter:
        backend_name = "stdout"
        target_window_id = None

        def commit(self, text: str) -> SimpleNamespace:
            return SimpleNamespace(backend="stdout", committed=True, detail="printed")

    class _FakeProcess:
        def poll(self) -> int:
            return 0

    def _fake_start_record_process(**kwargs) -> RecordProcessHandle:  # noqa: ANN003
        output_path = kwargs["output_path"]
        output_path.write_bytes(b"")
        return RecordProcessHandle(process=_FakeProcess(), monitor_stream=io.BytesIO(b""))

    monkeypatch.setattr("recordian.hotkey_dictate.ensure_ffmpeg_available", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("recordian.hotkey_dictate.choose_record_backend", lambda requested, ffmpeg_bin: "ffmpeg-pulse")
    monkeypatch.setattr("recordian.hotkey_dictate.resolve_committer", lambda backend: _FakeCommitter())
    monkeypatch.setattr("recordian.hotkey_dictate.create_provider", lambda args: _FakeProvider())
    monkeypatch.setattr("recordian.hotkey_dictate.get_focused_window_id", lambda: None)
    monkeypatch.setattr("recordian.hotkey_dictate.start_record_process", _fake_start_record_process)
    monkeypatch.setattr("recordian.hotkey_dictate.stop_record_process", lambda *args, **kwargs: None)

    start_recording, _, exit_daemon, stop_event = build_ptt_hotkey_handlers(
        args=_fake_ptt_args(),
        on_result=events.append,
        on_error=events.append,
        on_busy=events.append,
        on_state=events.append,
    )

    assert start_recording("voice_wake") is True
    t0 = time.perf_counter()
    exit_daemon()
    elapsed = time.perf_counter() - t0

    result_events = [event for event in events if event.get("event") == "result"]
    assert elapsed >= 0.07
    assert stop_event.is_set() is True
    assert len(result_events) == 1
    assert result_events[0]["result"]["text"] == "退出等待"
