import argparse
import socketserver
import threading
import time
from pathlib import Path

from recordian.remote_paste.agent import RemotePasteAgent
from recordian.remote_paste.client import send_remote_paste, send_remote_paste_from_args
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
