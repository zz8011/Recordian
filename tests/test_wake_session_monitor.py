import argparse
import io
from types import SimpleNamespace

from recordian.wake_session_monitor import (
    WakeSessionMonitorContext,
    _effective_wake_auto_stop_silence_s,
    _should_extend_last_speech_timestamp,
    start_wake_session_monitor,
)


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


def test_should_extend_last_speech_timestamp_only_after_speech_started() -> None:
    assert _should_extend_last_speech_timestamp(
        speech_detected_raw=False,
        speech_detected=False,
        speech_started=False,
    ) is False
    assert _should_extend_last_speech_timestamp(
        speech_detected_raw=True,
        speech_detected=False,
        speech_started=False,
    ) is False
    assert _should_extend_last_speech_timestamp(
        speech_detected_raw=True,
        speech_detected=True,
        speech_started=False,
    ) is True
    assert _should_extend_last_speech_timestamp(
        speech_detected_raw=True,
        speech_detected=False,
        speech_started=True,
    ) is True


def test_effective_wake_auto_stop_silence_honors_short_user_pause() -> None:
    assert _effective_wake_auto_stop_silence_s(0.0) == 0.5
    assert _effective_wake_auto_stop_silence_s(1.0) == 1.0
    assert _effective_wake_auto_stop_silence_s(1.5) == 1.5
    assert _effective_wake_auto_stop_silence_s(2.2) == 2.2


def test_persistent_soft_noise_cannot_keep_wake_recording_alive(monkeypatch) -> None:
    import sys
    import threading

    import numpy as np

    from recordian import wake_session_monitor as monitor

    clock = {'now': 10.0, 'last_vad': 10.0}
    class Stream:
        def read(self, size):
            clock['now'] += .064
            return np.full(1024, .01, dtype=np.float32).tobytes() if clock['now'] < 16 else b''
    class Vad:
        def __init__(self, level):
            pass
        def is_speech(self, data, sample_rate):
            if clock['now'] < 10.6:
                clock['last_vad'] = clock['now']
                return True
            return False
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(Vad=Vad))
    monkeypatch.setattr(monitor.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(monitor, '_is_soft_keepalive_speech_frame', lambda **kw: True)
    state = {'voice_session_active': True, 'voice_started_ts': 10.0, 'voice_last_speech_ts': 10.0}
    events = []
    stopped = threading.Event()
    context = WakeSessionMonitorContext(
        args=argparse.Namespace(input_device='default', debug_diagnostics=True, wake_use_webrtcvad=True,
            wake_auto_stop_silence_s=1.0, wake_owner_silence_extend_s=0.0, wake_owner_verify=False,
            wake_speech_confirm_s=.18, wake_min_speech_s=.5, wake_no_speech_timeout_s=4.0),
        record_handle=SimpleNamespace(monitor_stream=Stream(), monitor_sample_rate=16000,
            monitor_channels=1, process=SimpleNamespace(poll=lambda: 0)),
        provider=SimpleNamespace(), stop_event=threading.Event(), get_state=state.get,
        set_state=state.__setitem__, resolve_hotwords=lambda: [], stop_recording=lambda: stopped.set(),
        normalize_final_text=lambda text: text, on_state=events.append,
    )
    thread = start_wake_session_monitor(context)
    thread.join(timeout=2)
    assert not thread.is_alive() and stopped.wait(1)
    stops = [e for e in events if e['event'] == 'voice_wake_auto_stop']
    assert len(stops) == 1 and stops[0]['reason'] == 'silence'
    assert 1.0 <= clock['now'] - clock['last_vad'] <= 1.4
    assert 1.0 <= stops[0]['since_last_speech_s'] <= 1.07
