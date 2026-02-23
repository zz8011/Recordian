import argparse
import time

from recordian.hotkey_dictate import (
    _adaptive_vad_threshold,
    _commit_text,
    _expand_key_name,
    _merge_stream_text,
    _normalize_final_text,
    _pcm16le_to_f32,
    build_hotkey_handlers,
    parse_hotkey_spec,
)
from recordian.linux_dictate import DictateResult


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


def test_commit_text_handles_generic_exception() -> None:
    class _BrokenCommitter:
        backend_name = "broken"

        def commit(self, text: str):  # noqa: ANN001
            raise RuntimeError("boom")

    payload = _commit_text(_BrokenCommitter(), "你好")
    assert payload["backend"] == "broken"
    assert payload["committed"] is False
    assert "boom" in str(payload["detail"])


def test_adaptive_vad_threshold_clamped_range() -> None:
    base = 0.045
    assert abs(_adaptive_vad_threshold(base, 0.0) - 0.018) < 1e-6
    assert 0.018 <= _adaptive_vad_threshold(base, 0.01) <= base
    assert abs(_adaptive_vad_threshold(base, 0.05) - base) < 1e-6


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
