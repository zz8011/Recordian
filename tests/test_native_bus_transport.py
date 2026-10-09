"""Persistent GIO transport contracts; never touches the desktop bus."""
from __future__ import annotations

import ctypes
import importlib
import importlib.util
import json
import os
import select
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from recordian.exceptions import CommitError

ROOT = Path(__file__).resolve().parents[1]


def bus_module():
    assert importlib.util.find_spec("recordian.native_bus") is not None, (
        "production native D-Bus adapter is missing"
    )
    return importlib.import_module("recordian.native_bus")


def test_native_failure_never_replays_through_busctl(monkeypatch):
    module = bus_module()
    from recordian import linux_commit

    class FailedTransport:
        def call(self, *args, **kwargs):
            raise CommitError("native dispatched write timed out")

    monkeypatch.setattr(module, "get_transport", lambda: FailedTransport())
    monkeypatch.setattr(linux_commit.subprocess, "run", lambda *a, **k: pytest.fail("replay"))
    with pytest.raises(CommitError, match="timed out"):
        linux_commit._fcitx_busctl_call("CommitText", "s", ["hello"])


def test_unavailable_native_uses_existing_busctl(monkeypatch):
    module = bus_module()
    from recordian import linux_commit

    monkeypatch.setattr(module, "get_transport", lambda: None)
    monkeypatch.setattr(linux_commit, "which", lambda _: "/usr/bin/busctl")
    monkeypatch.setattr(linux_commit.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='s "committed fixture"', stderr=""))
    assert linux_commit._fcitx_busctl_call("CommitText", "s", ["hello"]) == "committed fixture"


def test_native_commit_does_not_require_busctl(monkeypatch):
    module = bus_module()
    from recordian import linux_commit

    monkeypatch.setattr(module, "get_transport", lambda: SimpleNamespace(
        call=lambda method, *a, **k: "ok" if method == "Ping" else "committed native"))
    monkeypatch.setattr(linux_commit, "which", lambda _: None)
    assert linux_commit.FcitxCommitter().commit("hello").committed
    assert linux_commit._fcitx_channel_available()


def test_python_selection_is_cached_before_any_native_call(monkeypatch):
    module = bus_module()
    monkeypatch.setattr(module, "_selection", module._UNSET)
    monkeypatch.setattr(module, "load_library", lambda: None)
    assert module.get_transport() is None
    monkeypatch.setattr(module, "load_library", lambda: pytest.fail("transport selection changed"))
    assert module.get_transport() is None


def test_required_loader_failure_never_selects_busctl(monkeypatch):
    module = bus_module()
    from recordian import linux_commit
    monkeypatch.setattr(module, "_selection", module._UNSET)

    def unavailable():
        raise RuntimeError("Recordian Rust core unavailable")

    monkeypatch.setattr(module, "load_library", unavailable)
    monkeypatch.setattr(linux_commit.subprocess, "run", lambda *a, **k: pytest.fail("fallback"))
    with pytest.raises(RuntimeError, match="unavailable"):
        linux_commit._fcitx_busctl_call("CommitText", "s", ["hello"])


def test_library_missing_transport_abi_fails_closed(monkeypatch):
    module = bus_module()
    monkeypatch.setattr(module, "_selection", module._UNSET)
    monkeypatch.setattr(module, "load_library", lambda: SimpleNamespace())
    with pytest.raises(CommitError, match="missing D-Bus ABI"):
        module.get_transport()
    monkeypatch.setattr(module, "load_library", lambda: pytest.fail("retried loader"))
    with pytest.raises(CommitError, match="missing D-Bus ABI"):
        module.get_transport()


def ready(process, expected):
    assert select.select([process.stdout], [], [], 5)[0], "fixture readiness timed out"
    assert process.stdout.readline().strip() == expected


@pytest.fixture
def private_bus(tmp_path):
    daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1",
                               f"--address=unix:path={tmp_path}/bus"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert select.select([daemon.stdout], [], [], 5)[0]
    address = daemon.stdout.readline().strip()
    services = []

    def start():
        log = tmp_path / f"calls-{len(services)}.jsonl"
        process = subprocess.Popen(["/usr/bin/python3", str(ROOT / "tests/native/dbus_transport_fixture.py"),
                                    address, str(log)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        services.append(process)
        ready(process, "READY")
        return process, log

    service, log = start()
    try:
        yield address, service, log, start
    finally:
        for process in [*services, daemon]:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()


@pytest.fixture
def transport(private_bus):
    module = bus_module()
    path = Path(os.environ.get("RECORDIAN_NATIVE_LIBRARY", str(
        ROOT / "native/recordian-core/target/release/librecordian_core.so")))
    if not path.is_file():
        pytest.skip("parent-built native library is not available yet")
    bus = module.NativeBus(ctypes.CDLL(str(path)), address=private_bus[0])
    try:
        yield bus
    finally:
        bus.close()


def calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def begin(transport):
    return transport.call("BeginSession", "s", [""]).split()[0]


def test_unicode_and_signatures_use_one_connection(transport, private_bus):
    text = '中文🙂 "quoted"\\\nnext line'
    assert transport.call("Ping", "", []) == "ok"
    token = begin(transport)
    assert transport.call("UpdatePreedit", "ss", [token, text]) == "updated segments=1"
    assert transport.call("CommitSegment", "sus", [token, "1", text]) == "segment 1 fixture"
    assert transport.call("CommitSession", "ss", [token, text]) == "committed fixture"
    observed = calls(private_bus[2])
    assert [x["method"] for x in observed] == ["Ping", "BeginSession", "UpdatePreedit",
                                                "CommitSegment", "CommitSession"]
    assert observed[2]["args"] == ["server-token", text]
    assert observed[3]["args"] == ["server-token", 1, text]
    assert len({x["sender"] for x in observed}) == 1


def test_wrong_reply_is_uncertain_and_cannot_replay(transport, private_bus):
    token = begin(transport)
    with pytest.raises(CommitError, match="InvalidReply"):
        transport.call("CommitSession", "ss", [token, "__wrong__"])
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [token, "retry"])
    assert transport.call("CancelSession", "s", [token]) == "cancelled"
    assert [x["method"] for x in calls(private_bus[2])] == ["BeginSession", "CommitSession", "CancelSession"]


def test_remote_error_retains_structured_name_and_buffered_session(transport):
    token = begin(transport)
    with pytest.raises(CommitError, match="org.fcitx.Fcitx.Recordian.Error.SegmentsUnsafe"):
        transport.call("CommitSegment", "sus", [token, "1", "__error__"])
    assert transport.call("CommitSession", "ss", [token, "buffered"]) == "committed fixture"


def test_timeout_has_one_mutation_and_one_cleanup(transport, private_bus):
    token = begin(transport)
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [token, "__timeout__"], timeout_ms=30)
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [token, "retry"])
    transport.call("CancelSession", "s", [token])
    with pytest.raises(CommitError):
        transport.call("CancelSession", "s", [token])
    assert [x["method"] for x in calls(private_bus[2])] == ["BeginSession", "CommitSession", "CancelSession"]


def test_owner_transfer_keeps_original_token_destination(transport, private_bus):
    token = begin(transport)
    old, old_log = private_bus[1:3]
    old.stdin.write("release\n")
    old.stdin.flush()
    ready(old, "RELEASED")
    _, replacement_log = private_bus[3]()
    transport.call("UpdatePreedit", "ss", [token, "old owner"])
    transport.call("CancelSession", "s", [token])
    assert calls(replacement_log) == []
    assert calls(old_log)[-2]["args"] == ["server-token", "old owner"]
    replacement_token = begin(transport)
    assert replacement_token != token  # even when the server reuses a raw token
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [token, "must never redirect"])
    assert [x["method"] for x in calls(replacement_log)] == ["BeginSession"]


def test_owner_loss_cleanup_cannot_target_replacement(transport, private_bus):
    token = begin(transport)
    old = private_bus[1]
    old.terminate()
    old.wait(timeout=3)
    _, replacement_log = private_bus[3]()
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [token, "lost owner"])
    with pytest.raises(CommitError):
        transport.call("CancelSession", "s", [token])
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [token, "no redirect"])
    assert calls(replacement_log) == []


def test_token_memory_is_bounded_and_cancel_releases_capacity(transport, private_bus):
    tokens = [begin(transport) for _ in range(256)]
    with pytest.raises(CommitError, match="SessionBusy"):
        begin(transport)
    assert len(calls(private_bus[2])) == 256  # refused before Begin dispatch
    transport.call("CancelSession", "s", [tokens[0]])
    replacement = begin(transport)
    assert replacement not in tokens
    with pytest.raises(CommitError):
        transport.call("CommitSession", "ss", [tokens[0], "retired"])
    assert len(calls(private_bus[2])) == 258


@pytest.mark.parametrize("method,signature", [
    ("UpdatePreedit", "ss"), ("CommitSegment", "sus"),
    ("CommitSession", "ss"), ("CancelSession", "s"),
])
def test_exact_stale_reply_releases_binding_beyond_capacity(transport, private_bus, method, signature):
    retired = []
    for _ in range(300):
        initial = "__stale_cancel__" if method == "CancelSession" else ""
        token = transport.call("BeginSession", "s", [initial]).split()[0]
        retired.append(token)
        args = [token]
        if method == "CommitSegment":
            args.append("1")
        if method != "CancelSession":
            args.append("__stale__")
        with pytest.raises(CommitError) as error:
            transport.call(method, signature, args)
        assert error.value.dbus_error_name == "org.fcitx.Fcitx.Recordian.Error.StaleSession"
    assert len(set(retired)) == 300
    observed = calls(private_bus[2])
    assert [row["method"] for row in observed] == ["BeginSession", method] * 300
    for token in (retired[0], retired[-1]):
        with pytest.raises(CommitError, match="UnknownToken"):
            transport.call("CommitSession", "ss", [token, "must not replay"])
        with pytest.raises(CommitError, match="UnknownToken"):
            transport.call("CancelSession", "s", [token])
    assert calls(private_bus[2]) == observed
    assert begin(transport) not in retired


def test_stale_lookalike_retains_binding_for_one_original_owner_cleanup(transport, private_bus):
    token = begin(transport)
    with pytest.raises(CommitError) as error:
        transport.call("CommitSession", "ss", [token, "__stale_lookalike__"])
    assert error.value.dbus_error_name == "org.fcitx.Fcitx.Recordian.Error.StaleSessionExtra"
    with pytest.raises(CommitError, match="SessionClosed"):
        transport.call("CommitSession", "ss", [token, "must not replay"])
    old = private_bus[1]
    old.stdin.write("release\n")
    old.stdin.flush()
    ready(old, "RELEASED")
    _, replacement_log = private_bus[3]()
    assert transport.call("CancelSession", "s", [token]) == "cancelled"
    with pytest.raises(CommitError, match="UnknownToken"):
        transport.call("CancelSession", "s", [token])
    assert [row["method"] for row in calls(private_bus[2])] == ["BeginSession", "CommitSession", "CancelSession"]
    assert calls(replacement_log) == []


@pytest.mark.parametrize("operation,method", [("commit", "CommitSession"),
                                               ("commit_segment", "CommitSegment")])
def test_python_session_stale_lookalike_cleans_original_owner_beyond_capacity(
    transport, private_bus, monkeypatch, operation, method,
):
    from recordian import linux_commit

    monkeypatch.setattr(bus_module(), "get_transport", lambda: transport)
    monkeypatch.setattr(linux_commit.subprocess, "run", lambda *a, **k: pytest.fail("busctl replay"))
    committer = linux_commit.FcitxCommitter()
    replacement_log = None
    for iteration in range(300):
        session = committer.begin_composition()
        if iteration == 0:
            old = private_bus[1]
            old.stdin.write("release\n")
            old.stdin.flush()
            ready(old, "RELEASED")
            _, replacement_log = private_bus[3]()
        result = getattr(session, operation)("__stale_lookalike__")
        assert result.outcome == "uncertain"
        assert not result.committed and not session.active
        assert "preedit_cancelled" in result.detail
        assert getattr(session, operation)("must not replay").outcome == "stale"
        assert session.commit("must not fall back").outcome == "stale"
        assert session.cancel().detail == "cancel_noop:closed"
    assert [row["method"] for row in calls(private_bus[2])] == ["BeginSession", method, "CancelSession"]
    assert calls(private_bus[2])[-1]["args"] == ["server-token"]
    assert [row["method"] for row in calls(replacement_log)] == ["BeginSession", method, "CancelSession"] * 299
    final = committer.begin_composition()
    assert final.active
    assert final.cancel().outcome == "cancelled"


def test_python_session_structured_error_cannot_be_overridden_by_segmentsunsafe_message(
    transport, private_bus, monkeypatch,
):
    from recordian import linux_commit

    monkeypatch.setattr(bus_module(), "get_transport", lambda: transport)
    session = linux_commit.FcitxCommitter().begin_composition()
    result = session.commit_segment("__unsafe_lookalike__")
    assert result.outcome == "uncertain"
    assert not session.active
    assert "preedit_cancelled" in result.detail
    assert session.commit("must not buffer").outcome == "stale"
    assert session.cancel().detail == "cancel_noop:closed"
    assert [row["method"] for row in calls(private_bus[2])] == ["BeginSession", "CommitSegment", "CancelSession"]


@pytest.mark.parametrize("operation,method", [("commit", "CommitSession"),
                                               ("commit_segment", "CommitSegment")])
@pytest.mark.parametrize("has_field,name,message,outcome", [
    (False, None, "StaleSession mentioned in prose", "uncertain"),
    (False, None, "(org.fcitx.Fcitx.Recordian.Error.StaleSession)", "stale"),
    (False, None, "org.fcitx.Fcitx.Recordian.Error.StaleSessionExtra", "uncertain"),
    (False, None, "org.fcitx.Fcitx.Recordian.Error.StaleSession.Extra", "uncertain"),
    (True, "", "org.fcitx.Fcitx.Recordian.Error.StaleSession", "uncertain"),
    (True, None, "org.fcitx.Fcitx.Recordian.Error.StaleSession", "uncertain"),
    (True, "org.other.StaleSession", "org.fcitx.Fcitx.Recordian.Error.StaleSession", "uncertain"),
    (True, "org.fcitx.Fcitx.Recordian.Error.StaleSessionExtra",
     "org.fcitx.Fcitx.Recordian.Error.StaleSession", "uncertain"),
    (True, "org.fcitx.Fcitx.Recordian.Error.StaleSession",
     "org.fcitx.Fcitx.Recordian.Error.SegmentsUnsafe", "stale"),
])
def test_python_session_structured_and_legacy_error_classification(
    monkeypatch, operation, method, has_field, name, message, outcome,
):
    from recordian import linux_commit

    error = CommitError(message)
    if has_field:
        error.dbus_error_name = name
    dispatched = []

    def call(member, signature, args):
        dispatched.append(member)
        if member == "CancelSession":
            assert signature == "s" and args == ["bound-token"]
            return "cancelled"
        raise error

    monkeypatch.setattr(linux_commit, "_fcitx_busctl_call", call)
    session = linux_commit.FcitxStreamingSession(
        linux_commit.FcitxCommitter(), "bound-token", preedit_capable=True,
        supports_segments=True,
    )
    assert getattr(session, operation)("text").outcome == outcome
    assert not session.active
    assert session.commit("must not replay").outcome == "stale"
    assert session.commit_segment("must not replay").outcome == "stale"
    assert session.cancel().detail == "cancel_noop:closed"
    assert dispatched == ([method] if outcome == "stale" else [method, "CancelSession"])


def test_native_session_segmentsunsafe_and_uncertain_behavior(transport, private_bus, monkeypatch):
    from recordian import linux_commit
    monkeypatch.setattr(bus_module(), "get_transport", lambda: transport)
    monkeypatch.setattr(linux_commit.subprocess, "run", lambda *a, **k: pytest.fail("busctl replay"))
    session = linux_commit.FcitxCommitter().begin_composition()
    assert session.commit_segment("__error__").outcome == "buffered"
    assert session.active and session.supports_buffered_continuous
    assert session.commit("buffered text").outcome == "committed"
    session = linux_commit.FcitxCommitter().begin_composition()
    assert session.commit("__wrong__").outcome == "uncertain"
    assert session.commit("retry").outcome == "stale"
    assert session.cancel().detail == "cancel_noop:closed"
    observed = calls(private_bus[2])
    assert [x["method"] for x in observed] == ["BeginSession", "CommitSegment", "CommitSession",
                                                "BeginSession", "CommitSession", "CancelSession"]
    assert observed[-1]["args"] == ["server-token"]


def test_closed_connection_rejects_without_dispatch(transport, private_bus):
    transport.close()
    with pytest.raises(CommitError, match="SessionClosed"):
        transport.call("CommitText", "s", ["must not write"])
    assert calls(private_bus[2]) == []


@pytest.mark.parametrize("method,signature,args", [
    ("DeleteAll", "", []), ("CommitText", "u", ["1"]),
    ("CommitText", "s", ["nul\0text"]), ("CommitText", "s", []),
    ("CommitText", "s", ["\ud800"]),
    ("CommitText", "s", ["x" * 1_048_577]),
    ("CommitSegment", "sus", ["unknown", "-1", "text"]),
    ("CommitSegment", "sus", ["unknown", "4294967296", "text"]),
    ("CommitSession", "ss", ["unknown", "text"]),
])
def test_invalid_calls_do_not_dispatch(transport, private_bus, method, signature, args):
    with pytest.raises(CommitError):
        transport.call(method, signature, args)
    assert calls(private_bus[2]) == []
