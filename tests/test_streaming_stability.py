"""Streaming failure regressions using only synthetic PCM and fake transports."""
import threading

import pytest
from test_confucius_asr import FakeWebSocket, _connected_greeting, _f32_frame, _make_session, _success
from test_continuous_dictation import (
    _FakeASRSession,
    _FakeProvider,
    _Harness,
    _Reader,
    _short_segment_settings,
    _silence,
    _speech,
)

from recordian.providers.confucius_asr import f32le_to_pcm16le


@pytest.mark.parametrize("method", ["push_audio", "push_pcm16"])
def test_stop_drains_pcm_admitted_before_eos(monkeypatch, method):
    entered = threading.Event()
    release = threading.Event()
    eos = threading.Event()
    ws = FakeWebSocket(
        greeting=_connected_greeting(),
        on_eos=lambda sock: (eos.set(), sock.feed(_success("终稿", reset=True)), sock.feed_close(1000)),
    )
    session = _make_session(ws)
    original_enqueue = session._enqueue

    def gated_enqueue(frame, **kwargs):
        if frame is not None:
            entered.set()
            assert release.wait(2), "test did not release audio admission"
        return original_enqueue(frame, **kwargs)

    monkeypatch.setattr(session, "_enqueue", gated_enqueue)
    pcm = f32le_to_pcm16le(_f32_frame())
    results = {}

    def run(name, action):
        try:
            results[name] = action()
        except Exception as exc:
            results[name] = exc

    payload = _f32_frame() if method == "push_audio" else pcm
    push = threading.Thread(target=run, args=("push", lambda: getattr(session, method)(payload)))
    stop = threading.Thread(target=run, args=("finish", session.finish))
    try:
        push.start()
        assert entered.wait(1)
        stop.start()
        # On the broken path EOS completes while accepted PCM is still entering.
        # On the fixed path the same admission barrier holds EOS until release.
        eos.wait(0.2)
        release.set()
        push.join(2)
        stop.join(2)
        assert not push.is_alive() and not stop.is_alive()
        assert not isinstance(results["push"], Exception)
        assert not isinstance(results["finish"], Exception)
        assert ws.sent_binary == [pcm], "accepted PCM was lost behind the EOS sentinel"
        assert results["finish"].text == "终稿"
    finally:
        release.set()
        session.cancel()
        if push.ident is not None:
            push.join(2)
        if stop.ident is not None:
            stop.join(2)


@pytest.mark.parametrize("reset", ["false", "true", 1, {"reset": False}, [False]])
def test_nonboolean_reset_cannot_confirm_final(reset):
    message = _success("不可确认的预览")
    message["msg"]["reset"] = reset
    ws = FakeWebSocket(
        greeting=_connected_greeting(),
        on_eos=lambda sock: (sock.feed(message), sock.feed_close(1000)),
    )
    session = _make_session(ws)
    with pytest.raises(RuntimeError, match="reset"):
        session.finish()


def test_failed_tail_after_confirmed_segment_remains_recoverable(monkeypatch):
    _short_segment_settings(monkeypatch)
    provider = _FakeProvider(["第一段。", "未确认尾巴"], partials=["第一段。", "未确认尾巴"])
    harness = _Harness(reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)), provider=provider)
    finish = _FakeASRSession.finish

    def fail_tail(self):
        if self.final_text == "未确认尾巴":
            raise ConnectionError("synthetic disconnect")
        return finish(self)

    monkeypatch.setattr(_FakeASRSession, "finish", fail_tail)
    worker = harness.run()
    assert harness.session.segments == ["第一段。"]
    assert harness.session.commits == []
    assert worker.outcome == "uncertain"
    assert worker.final_text == "第一段。未确认尾巴"
    assert worker.commit_info["recovery_text"] == "未确认尾巴"
    assert worker.commit_info["incomplete"] is True
    assert len(provider.open_calls) == 2, "an interrupted tail must never reopen or replay"


@pytest.mark.parametrize("supports_segments", [True, False])
def test_full_preview_survives_failure_beyond_preedit_limit(supports_segments):
    from test_continuous_dictation import _FakeCompositionSession, _Result

    preview = "".join(f"syntheticitem{chr(65 + i // 26)}{chr(65 + i % 26)}。" for i in range(200))
    session = _FakeCompositionSession()
    session.supports_segments = supports_segments
    session.preedit_result = _Result(False, outcome="stale", detail="preedit_stale")
    provider = _FakeProvider(["终稿"], partials=[preview])
    worker = _Harness(reader=_Reader(_speech(0.5)), provider=provider, session=session).run()
    assert len(session.preedits[0]) <= 512
    assert worker.final_text == preview
    assert worker.commit_info["recovery_text"] == preview
    assert session.commits == []


def test_uncertain_tail_commit_keeps_preview_without_retry(monkeypatch):
    from test_continuous_dictation import _FakeCompositionSession, _Result

    _short_segment_settings(monkeypatch)
    session = _FakeCompositionSession()
    session.commit_result = _Result(False, outcome="uncertain", detail="reply_lost")
    provider = _FakeProvider(["第一段。", "尾段。"])
    worker = _Harness(
        reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)),
        provider=provider, session=session,
    ).run()
    assert session.segments == ["第一段。"]
    assert session.commits == ["尾段。"], "ambiguous commit must never be repeated"
    assert worker.final_text == "第一段。尾段。"
    assert worker.commit_info["recovery_text"] == "尾段。"
    assert worker.commit_info["incomplete"] is True


def test_partial_prefix_result_reports_incomplete_in_fake_tray():
    from test_duration_guard import _handle, _make_fake_tray

    fake, overlay_calls, notifications, _ = _make_fake_tray()
    _handle(fake, {"event": "result", "result": {
        "text": "第一段。尾段。", "asr_path": "prefetched",
        "commit": {"backend": "fcitx", "committed": True,
                   "outcome": "uncertain", "incomplete": True,
                   "recovery_text": "尾段。", "segments_committed": 1},
    }})
    assert fake.state.status == "error"
    assert "核对" in fake.state.detail
    assert fake.state.last_run.text == "第一段。尾段。"
    assert overlay_calls[-1][0] == "error"
    assert len(notifications) == 1
    assert "核对" in notifications[0][1]
    assert "尾段。" not in notifications[0][1], "notification must not contain transcript"


def test_socket_reopen_failure_preserves_held_tail(monkeypatch):
    _short_segment_settings(monkeypatch)
    provider = _FakeProvider(["前文一百二"])
    provider.fail_on_open[1] = ConnectionError("synthetic reopen failure")
    worker = _Harness(
        reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)), provider=provider,
    ).run()
    assert worker.segments_committed == 1
    assert worker.final_text == "前文一百二"
    assert worker.commit_info["recovery_text"] == "一百二"


def test_uncertain_segment_preserves_its_held_tail(monkeypatch):
    from test_continuous_dictation import _FakeCompositionSession

    _short_segment_settings(monkeypatch)
    provider = _FakeProvider(["前文一百二"])
    session = _FakeCompositionSession()
    session.fail_segment_at = 1
    worker = _Harness(
        reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(0.5)), provider=provider, session=session,
    ).run()
    assert session.segments == ["前文"]
    assert worker.final_text == "前文一百二"
    assert worker.commit_info["recovery_text"] == "前文一百二"


@pytest.mark.parametrize("mode", ["success", "disconnect", "cancel"])
def test_twenty_fresh_sessions_recover_without_threads_or_eos_replay(mode):
    from recordian.providers.confucius_asr import EOS_MESSAGE

    for _ in range(20):
        def reply(sock):
            if mode == "disconnect":
                sock.feed_drop()
            else:
                sock.feed(_success("终稿", reset=True))
                sock.feed_close(1000)
        ws = FakeWebSocket(greeting=_connected_greeting(), on_eos=reply)
        session = _make_session(ws)
        session.push_audio(_f32_frame())
        if mode == "cancel":
            session.cancel()
            session.cancel()
            with pytest.raises(RuntimeError, match="cancel"):
                session.finish()
        elif mode == "disconnect":
            with pytest.raises(RuntimeError, match="1006"):
                session.finish()
        else:
            assert session.finish().text == "终稿"
            assert session.finish().text == "终稿"
        session.cancel()
        assert ws.sent_text.count(EOS_MESSAGE) == (0 if mode == "cancel" else 1)
        assert not session._sender_thread.is_alive()
        assert not session._receiver_thread.is_alive()


def test_finish_admission_wait_uses_original_deadline():
    import time

    from recordian.providers.confucius_asr import EOS_MESSAGE

    ws = FakeWebSocket(greeting=_connected_greeting())
    session = _make_session(ws)
    session._timeout_s = 0.05
    session._admission_lock.acquire()
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="admission"):
            session.finish()
        assert time.monotonic() - start < 0.5
        assert EOS_MESSAGE not in ws.sent_text
    finally:
        session._admission_lock.release()
        session.cancel()


def test_buffered_downgrade_after_prefix_does_not_duplicate_recovery(monkeypatch):
    from test_continuous_dictation import _FakeCompositionSession

    _short_segment_settings(monkeypatch)
    session = _FakeCompositionSession()
    original_commit = session.commit_segment
    def downgrade(text):
        result = original_commit(text)
        session.supports_segments = False
        return result
    session.commit_segment = downgrade
    provider = _FakeProvider(
        ["第一段。", "缓冲段。", "待核对尾巴"], partials=["第一段。", "缓冲段。", "待核对尾巴"],
    )
    original_finish = _FakeASRSession.finish
    def disconnect_tail(self):
        if self.final_text == "待核对尾巴":
            raise ConnectionError("synthetic disconnect")
        return original_finish(self)
    monkeypatch.setattr(_FakeASRSession, "finish", disconnect_tail)
    worker = _Harness(
        reader=_Reader(_speech(1.5) + _silence(0.4) + _speech(1.5) + _silence(0.4) + _speech(0.5)),
        provider=provider, session=session,
    ).run()
    assert session.segments == ["第一段。"]
    assert session.commits == []
    assert worker.final_text == "第一段。缓冲段。待核对尾巴"
    assert worker.commit_info["recovery_text"] == "缓冲段。待核对尾巴"
