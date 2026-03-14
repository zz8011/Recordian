import argparse
import io
from types import SimpleNamespace

from recordian.wake_session_monitor import WakeSessionMonitorContext, start_wake_session_monitor


def test_start_wake_session_monitor_exits_when_monitor_stream_ends() -> None:
    state: dict[str, object] = {
        "voice_session_active": False,
        "voice_semantic_enabled": False,
        "voice_owner_filter_enabled": False,
        "voice_owner_active": True,
        "voice_owner_seen": False,
        "voice_owner_last_score": -1.0,
    }
    events: list[dict[str, object]] = []

    context = WakeSessionMonitorContext(
        args=argparse.Namespace(
            input_device="default",
            debug_diagnostics=False,
            wake_use_webrtcvad=False,
            wake_vad_aggressiveness=2,
            wake_vad_frame_ms=30,
            wake_speech_confirm_s=0.18,
            wake_semantic_probe_interval_s=0.45,
            wake_semantic_window_s=1.2,
            wake_semantic_end_silence_s=1.0,
            wake_semantic_min_chars=1,
            wake_semantic_timeout_ms=1200,
            wake_owner_threshold=0.72,
            wake_owner_window_s=1.6,
            wake_owner_verify=False,
            wake_no_speech_timeout_s=2.0,
            wake_min_speech_s=0.5,
            wake_auto_stop_silence_s=1.5,
            wake_owner_silence_extend_s=0.5,
        ),
        record_handle=SimpleNamespace(
            monitor_stream=io.BytesIO(b""),
            monitor_sample_rate=16000,
            monitor_channels=1,
            process=SimpleNamespace(poll=lambda: 0),
        ),
        provider=SimpleNamespace(),
        stop_event=SimpleNamespace(is_set=lambda: False, wait=lambda timeout=0.0: False),
        get_state=state.get,
        set_state=state.__setitem__,
        resolve_hotwords=lambda: [],
        stop_recording=lambda: True,
        normalize_final_text=lambda text: str(text).strip(),
        on_state=events.append,
    )

    thread = start_wake_session_monitor(context)
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert state["voice_owner_filter_enabled"] is False
    assert state["voice_owner_active"] is True
