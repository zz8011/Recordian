"""CommitSegment client contract: one token, monotonic sequence, no retry.

The addon owns the write. These tests stub busctl and check that the
Python session never repeats a sequence, never skips ahead after a
failure, and never falls back to CommitSession or CommitText.
"""
from __future__ import annotations

from types import SimpleNamespace

IFACE = "org.fcitx.Fcitx.Recordian1"


def _method_of(cmd: list[str]) -> str:
    return cmd[cmd.index(IFACE) + 1]


def _segment_call(cmd: list[str]) -> tuple[str, str, str]:
    at = cmd.index("sus")
    token, sequence, text = cmd[at + 1 : at + 4]
    return token, sequence, text


def _install(monkeypatch, run) -> None:
    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr("recordian.linux_commit.subprocess.run", run)


def _ok(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


def test_begin_exposes_segments_marker(monkeypatch) -> None:
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _ok('s "abc123 preedit=1 frontend=gtk3 program=gedit segments=1"')

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    assert session.supports_segments is True
    assert session.token == "abc123"
    assert session.preedit_capable is True
    assert session.active


def test_old_bridge_without_marker_keeps_short_session(monkeypatch) -> None:
    """No segments=1 means commit_segment does not touch the bus."""
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []
    replies = [
        's "abc123 preedit=1 frontend=gtk3 program=gedit"',
        's "committed gtk3 gedit"',
    ]

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _ok(replies.pop(0))

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    assert session.supports_segments is False
    refused = session.commit_segment("不应发送")
    assert refused.committed is False
    assert refused.outcome == "stale"
    assert refused.detail == "segments_unsupported"
    assert session.active
    done = session.commit("整句")
    assert done.committed is True
    assert [_method_of(cmd) for cmd in calls] == ["BeginSession", "CommitSession"]


def test_segments_commit_same_token_then_final_once(monkeypatch) -> None:
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []
    replies = [
        's "tokenseg preedit=1 frontend=gtk3 program=p segments=1"',
        's "segment 1 gtk3 p"',
        's "segment 2 gtk3 p"',
        's "committed gtk3 p"',
    ]

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _ok(replies.pop(0))

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("草稿")
    token = session.token
    first = session.commit_segment("第一段")
    second = session.commit_segment("第二段")
    assert first.committed and second.committed
    assert first.outcome == "committed"
    assert session.active
    assert session.token == token
    final = session.commit("收尾")
    again = session.commit("第二次收尾")
    assert final.committed is True
    assert final.detail == "committed gtk3 p"
    assert again.committed is False
    assert again.outcome == "stale"
    assert not session.active
    methods = [_method_of(cmd) for cmd in calls]
    assert methods == ["BeginSession", "CommitSegment", "CommitSegment", "CommitSession"]
    assert _segment_call(calls[1]) == (token, "1", "第一段")
    assert _segment_call(calls[2]) == (token, "2", "第二段")
    assert calls[3][calls[3].index("ss") + 1] == token
    # The closed client must not send another commit or a plain CommitText.
    assert "CommitText" not in methods
    assert methods.count("CommitSession") == 1


def test_segment_lost_reply_is_terminal_uncertain(monkeypatch) -> None:
    """A timeout may already have written. Never retry, reopen, or fall back."""
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "BeginSession":
            return _ok('s "tokenseg preedit=0 frontend=gtk3 program=p segments=1"')
        if method == "CommitSegment":
            raise TimeoutError("busctl timed out")
        if method == "CancelSession":
            return _ok('s "cancelled"')
        raise AssertionError(f"unexpected bus call {method}")

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    lost = session.commit_segment("可能已写入")
    assert lost.committed is False
    assert lost.outcome == "uncertain"
    assert "preedit_cancelled" in lost.detail
    assert not session.active
    # Closed: further segment, commit, and cancel stay local.
    again = session.commit_segment("重试应被丢掉")
    final = session.commit("兜底应被丢掉")
    cancel = session.cancel()
    assert again.outcome == "stale"
    assert final.outcome == "stale"
    assert cancel.detail == "cancel_noop:closed"
    methods = [_method_of(cmd) for cmd in calls]
    assert methods == ["BeginSession", "CommitSegment", "CancelSession"]
    assert _segment_call(calls[1])[1] == "1"
    assert "CommitText" not in methods
    assert "CommitSession" not in methods


def test_bad_sequence_closes_without_retry(monkeypatch) -> None:
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "BeginSession":
            return _ok('s "tokenseg preedit=1 frontend=gtk3 program=p segments=1"')
        if method == "CommitSegment":
            return _fail(
                "Call failed: duplicate or out-of-order segment "
                "(org.fcitx.Fcitx.Recordian.Error.BadSequence)"
            )
        if method == "CancelSession":
            return _ok('s "cancelled"')
        raise AssertionError(method)

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    refused = session.commit_segment("第一段")
    assert refused.committed is False
    assert refused.outcome == "stale"
    assert "BadSequence" in refused.detail
    assert "preedit_cancelled" in refused.detail
    assert not session.active
    session.commit_segment("再试")
    session.commit("兜底")
    methods = [_method_of(cmd) for cmd in calls]
    assert methods == ["BeginSession", "CommitSegment", "CancelSession"]
    assert "CommitText" not in methods


def test_stale_segment_closes_without_cancel_or_fallback(monkeypatch) -> None:
    """Focus, typing, reset, and foreign preedit arrive as StaleSession.

    The addon already dropped the token, so the client must not cancel
    again and must not commit by another method.
    """
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "BeginSession":
            return _ok('s "tokenseg preedit=1 frontend=gtk3 program=p segments=1"')
        if method == "CommitSegment":
            return _fail(
                "Call failed: bound input context lost focus or is gone "
                "(org.fcitx.Fcitx.Recordian.Error.StaleSession)"
            )
        raise AssertionError(method)

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    lost = session.commit_segment("失焦段")
    assert lost.outcome == "stale"
    assert lost.committed is False
    assert not session.active
    session.commit_segment("回来再写")
    session.commit("兜底")
    methods = [_method_of(cmd) for cmd in calls]
    assert methods == ["BeginSession", "CommitSegment"]
    assert "CancelSession" not in methods
    assert "CommitText" not in methods
    assert "CommitSession" not in methods


def test_unknown_segment_reply_does_not_advance_a_second_call(monkeypatch) -> None:
    """A success-shaped body is required. Anything else is uncertain."""
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "BeginSession":
            return _ok('s "tokenseg preedit=1 frontend=gtk3 program=p segments=1"')
        if method == "CommitSegment":
            return _ok('s "committed gtk3 p"')
        if method == "CancelSession":
            return _fail("Call failed: connection closed")
        raise AssertionError(method)

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    odd = session.commit_segment("不像分段回复")
    assert odd.outcome == "uncertain"
    assert "preedit_may_linger" in odd.detail
    assert not session.active
    session.commit_segment("下一个序号也不发")
    assert [_method_of(cmd) for cmd in calls].count("CommitSegment") == 1


def test_wrong_sequence_ack_is_terminal_and_does_not_fall_through(monkeypatch) -> None:
    """A reply for a different sequence is not this call's commit.

    The client must close, cancel once, and must not send CommitSession,
    CommitText, or another CommitSegment.
    """
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "BeginSession":
            return _ok('s "tokenseg preedit=1 frontend=gtk3 program=p segments=1"')
        if method == "CommitSegment":
            # Sent sequence is 1. These bodies must all be rejected.
            sent = _segment_call(cmd)[1]
            assert sent == "1"
            return _ok('s "segment 2 gtk3 p"')
        if method == "CancelSession":
            return _ok('s "cancelled"')
        raise AssertionError(method)

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    wrong = session.commit_segment("第一段")
    assert wrong.committed is False
    assert wrong.outcome == "uncertain"
    assert "segment_failed" in wrong.detail
    assert "preedit_cancelled" in wrong.detail
    assert not session.active
    session.commit_segment("重试")
    session.commit("兜底整句")
    methods = [_method_of(cmd) for cmd in calls]
    assert methods == ["BeginSession", "CommitSegment", "CancelSession"]
    assert "CommitText" not in methods
    assert "CommitSession" not in methods


def test_longer_sequence_prefix_is_not_this_ack(monkeypatch) -> None:
    from recordian.linux_commit import FcitxCommitter

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "BeginSession":
            return _ok('s "tokenseg preedit=1 frontend=gtk3 program=p segments=1"')
        if method == "CommitSegment":
            return _ok('s "segment 12 gtk3 p"')
        if method == "CancelSession":
            return _ok('s "cancelled"')
        raise AssertionError(method)

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    wrong = session.commit_segment("第一段")
    assert wrong.outcome == "uncertain"
    assert not session.active
    assert [_method_of(cmd) for cmd in calls].count("CommitSegment") == 1
    assert "CommitText" not in [_method_of(cmd) for cmd in calls]


def test_segments_marker_must_be_exact_field(monkeypatch) -> None:
    from recordian.linux_commit import FcitxCommitter

    def _run(cmd, **kwargs):
        return _ok('s "tokenseg preedit=1 frontend=gtk3 program=p segments=10"')

    _install(monkeypatch, _run)
    session = FcitxCommitter().begin_composition("")
    assert session.supports_segments is False
