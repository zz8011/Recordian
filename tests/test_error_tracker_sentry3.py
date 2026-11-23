"""sentry-sdk 3.x 真实 API 回归：init 不抛 TypeError，带 context 的上报不走已移除的 push_scope。"""
import pytest

sentry_sdk = pytest.importorskip("sentry_sdk")
from sentry_sdk.transport import Transport  # noqa: E402

from recordian.error_tracker import ErrorTracker  # noqa: E402


def test_capture_with_context_reaches_transport() -> None:
    envelopes: list = []

    class _T(Transport):
        def capture_envelope(self, envelope) -> None:
            envelopes.append(envelope)

    tracker = ErrorTracker(dsn="http://k@localhost:1/1")
    assert tracker._initialized
    sentry_sdk.get_client().transport = _T()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        tracker.capture_exception(exc, ctx={"a": 1})
    tracker.capture_message("hi", ctx={"b": 2})
    assert len(envelopes) == 2
