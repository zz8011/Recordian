import argparse
import json
import socketserver
import threading
import time
from pathlib import Path

from recordian.remote_paste.agent import RemotePasteAgent
from recordian.remote_paste.client import resolve_remote_paste_routing, send_remote_paste, send_remote_paste_from_args
from recordian.remote_paste.config import load_agent_config
from recordian.remote_paste.protocol import decode_message, encode_message


class _LineHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        payload = decode_message(raw)
        assert payload["action"] == "paste"
        self.server.received = payload  # type: ignore[attr-defined]
        self.wfile.write(encode_message({"status": "ok", "hostname": "test-host", "detail": "queued"}))
        self.wfile.flush()


class _TestServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def test_send_remote_paste_uses_newline_delimited_json() -> None:
    with _TestServer(("127.0.0.1", 0), _LineHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        result = send_remote_paste(host, "跨电脑粘贴", port=port, timeout_s=1.0)
        server.shutdown()
        thread.join(timeout=1.0)

    assert result.ok is True
    assert result.status == "ok"
    assert server.received["text"] == "跨电脑粘贴"  # type: ignore[attr-defined]


def test_send_remote_paste_from_args_skips_when_disabled() -> None:
    args = argparse.Namespace(
        enable_remote_paste=False,
        remote_paste_host="192.168.5.111",
        remote_paste_port=24872,
        remote_paste_timeout_s=3.0,
    )
    result = send_remote_paste_from_args(args, "hello")
    assert result["attempted"] is False
    assert result["sent"] is False
    assert result["detail"] == "disabled"


def test_send_remote_paste_from_args_shared_clipboard_mode(monkeypatch) -> None:
    calls: list[object] = []

    monkeypatch.setattr("recordian.remote_paste.client._set_clipboard_text", lambda text: calls.append(("clipboard", text)))
    monkeypatch.setattr(
        "recordian.remote_paste.client._send_remote_command",
        lambda host, payload, *, port, timeout_s: calls.append(("payload", payload)) or type(
            "Result",
            (),
            {
                "ok": True,
                "status": "ok",
                "detail": "paste_only:ctrl+v",
                "response": {"status": "ok", "detail": "paste_only:ctrl+v", "payload": payload},
            },
        )(),
    )

    args = argparse.Namespace(
        enable_remote_paste=True,
        remote_paste_host="192.168.5.111",
        remote_paste_port=24872,
        remote_paste_timeout_s=3.0,
        remote_paste_mode="shared-clipboard",
        remote_paste_sync_wait_s=0.4,
    )
    result = send_remote_paste_from_args(args, "共享剪贴板测试")

    assert result["sent"] is True
    assert result["mode"] == "shared-clipboard"
    assert calls[0] == ("clipboard", "共享剪贴板测试")
    assert calls[1][0] == "payload"
    payload = calls[1][1]
    assert payload["action"] == "paste_only"
    assert payload["expected_text"] == "共享剪贴板测试"
    assert payload["clipboard_wait_s"] == 0.4


def test_resolve_remote_paste_routing_remote_only_when_deskflow_active_screen_matches(tmp_path: Path) -> None:
    state_path = tmp_path / "active_screen.json"
    state_path.write_text(
        json.dumps({"screen": "remote-screen", "server_name": "server-screen", "updated_at": "2026-03-15T00:00:00Z"}),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        enable_remote_paste=True,
        remote_paste_follow_deskflow_active_screen=True,
        deskflow_active_screen_path=str(state_path),
        deskflow_log_path="",
        remote_paste_screen_name="remote-screen",
    )
    routing = resolve_remote_paste_routing(args)

    assert routing.mode == "remote-only"
    assert routing.commit_local is False
    assert routing.send_remote is True
    assert routing.active_screen == "remote-screen"


def test_resolve_remote_paste_routing_falls_back_to_deskflow_log(tmp_path: Path) -> None:
    log_path = tmp_path / "deskflow-daemon.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-03-15T03:00:00 INFO switch from \"server-screen\" to \"remote-screen\" at 10,20",
                "2026-03-15T03:00:02 INFO switch from \"remote-screen\" to \"server-screen\" at 10,20",
                "2026-03-15T03:00:05 INFO switch from \"server-screen\" to \"remote-screen\" at 10,20",
            ]
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        enable_remote_paste=True,
        remote_paste_follow_deskflow_active_screen=True,
        deskflow_active_screen_path=str(tmp_path / "missing.json"),
        deskflow_log_path=str(log_path),
        remote_paste_screen_name="remote-screen",
    )
    routing = resolve_remote_paste_routing(args)

    assert routing.mode == "remote-only"
    assert routing.commit_local is False
    assert routing.send_remote is True
    assert routing.active_screen == "remote-screen"
    assert routing.deskflow_source == "log-file"


def test_send_remote_paste_from_args_skips_remote_when_deskflow_active_screen_is_local(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "active_screen.json"
    state_path.write_text(
        json.dumps({"screen": "server-screen", "server_name": "server-screen", "updated_at": "2026-03-15T00:00:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "recordian.remote_paste.client._send_remote_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not send remote command")),
    )

    args = argparse.Namespace(
        enable_remote_paste=True,
        remote_paste_host="192.168.5.111",
        remote_paste_port=24872,
        remote_paste_timeout_s=3.0,
        remote_paste_mode="direct",
        remote_paste_sync_wait_s=0.35,
        remote_paste_follow_deskflow_active_screen=True,
        deskflow_active_screen_path=str(state_path),
        deskflow_log_path="",
        remote_paste_screen_name="remote-screen",
    )
    result = send_remote_paste_from_args(args, "只应本地上屏")

    assert result["attempted"] is False
    assert result["sent"] is False
    assert result["status"] == "skipped"
    assert result["routing_mode"] == "local-only"
    assert result["detail"] == "deskflow_local_screen_active"


def test_load_agent_config_supports_flat_yaml_subset(tmp_path: Path) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text(
        "\n".join(
            [
                'hostname: "电脑B"',
                "port: 24872",
                "enable_notify: true",
                "paste_delay_ms: 150",
                'log_level: "DEBUG"',
            ]
        ),
        encoding="utf-8",
    )

    payload = load_agent_config(path)

    assert payload["hostname"] == "电脑B"
    assert payload["port"] == 24872
    assert payload["enable_notify"] is True
    assert payload["paste_delay_ms"] == 150
    assert payload["log_level"] == "DEBUG"


def test_agent_resolves_committer_from_current_focused_window(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _Committer:
        backend_name = "xdotool-clipboard"

        def commit(self, text: str):  # noqa: ANN001
            calls["text"] = text
            return type("CommitResult", (), {"committed": True, "detail": "paste:ctrl+shift+v"})()

    monkeypatch.setattr("recordian.remote_paste.agent.get_focused_window_id", lambda: 4242)
    monkeypatch.setattr(
        "recordian.remote_paste.agent.resolve_committer",
        lambda backend, *, target_window_id=None: calls.update(
            {"backend": backend, "target_window_id": target_window_id}
        ) or _Committer(),
    )

    agent = RemotePasteAgent(
        argparse.Namespace(
            hostname="remote-host",
            enable_notify=False,
            notify_backend="none",
            paste_delay_ms=0,
            commit_backend="auto",
        )
    )
    response = agent.handle_payload({"action": "paste", "text": "hello"})

    assert response["status"] == "ok"
    assert calls["backend"] == "auto"
    assert calls["target_window_id"] == 4242
    assert calls["text"] == "hello"


def test_agent_handles_paste_only(monkeypatch) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr("recordian.remote_paste.agent.get_focused_window_id", lambda: 4343)
    monkeypatch.setattr("recordian.remote_paste.agent._get_clipboard_text", lambda: "测试")
    monkeypatch.setattr(
        "recordian.remote_paste.agent.send_paste_shortcut",
        lambda *, target_window_id=None: calls.update({"target_window_id": target_window_id}) or type(
            "CommitResult",
            (),
            {"committed": True, "detail": "paste_only:ctrl+v"},
        )(),
    )

    agent = RemotePasteAgent(
        argparse.Namespace(
            hostname="remote-host",
            enable_notify=False,
            notify_backend="none",
            paste_delay_ms=0,
            commit_backend="auto",
        )
    )
    response = agent.handle_payload({"action": "paste_only", "preview": "测试", "expected_text": "测试", "clipboard_wait_s": 0.1})

    assert response["status"] == "ok"
    assert calls["target_window_id"] == 4343


def test_agent_rejects_paste_only_when_clipboard_not_synced(monkeypatch) -> None:
    monkeypatch.setattr("recordian.remote_paste.agent._get_clipboard_text", lambda: "旧内容")
    monkeypatch.setattr("recordian.remote_paste.agent.time.sleep", lambda seconds: None)

    agent = RemotePasteAgent(
        argparse.Namespace(
            hostname="remote-host",
            enable_notify=False,
            notify_backend="none",
            paste_delay_ms=0,
            commit_backend="auto",
        )
    )
    response = agent.handle_payload({"action": "paste_only", "preview": "新内容", "expected_text": "新内容", "clipboard_wait_s": 0.1})

    assert response["status"] == "error"
    assert response["detail"] == "clipboard_not_synced"


def test_agent_serializes_concurrent_paste_requests(monkeypatch) -> None:
    events: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()

    class _Committer:
        backend_name = "xdotool-clipboard"

        def __init__(self, text_label: str) -> None:
            self.text_label = text_label

        def commit(self, text: str):  # noqa: ANN001
            events.append(f"start:{text}")
            if text == "one":
                first_started.set()
                release_first.wait(timeout=1.0)
            events.append(f"end:{text}")
            return type("CommitResult", (), {"committed": True, "detail": self.text_label})()

    monkeypatch.setattr("recordian.remote_paste.agent.get_focused_window_id", lambda: 4242)
    monkeypatch.setattr(
        "recordian.remote_paste.agent.resolve_committer",
        lambda backend, *, target_window_id=None: _Committer(str(target_window_id)),
    )

    agent = RemotePasteAgent(
        argparse.Namespace(
            hostname="remote-host",
            enable_notify=False,
            notify_backend="none",
            paste_delay_ms=0,
            commit_backend="auto",
        )
    )

    responses: list[dict[str, object]] = []

    def _run(text: str) -> None:
        responses.append(agent.handle_payload({"action": "paste", "text": text}))

    t1 = threading.Thread(target=_run, args=("one",), daemon=True)
    t2 = threading.Thread(target=_run, args=("two",), daemon=True)
    t1.start()
    assert first_started.wait(timeout=1.0) is True
    t2.start()
    time.sleep(0.05)
    assert events == ["start:one"]
    release_first.set()
    t1.join(timeout=1.0)
    t2.join(timeout=1.0)

    assert events == ["start:one", "end:one", "start:two", "end:two"]
    assert len(responses) == 2
    assert all(response["status"] == "ok" for response in responses)
