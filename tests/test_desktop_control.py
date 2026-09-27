import socket
import threading

import pytest

from recordian.desktop_control import ControlServer, send_action


def test_socket_dispatches_press_release_once_and_cleans_up(tmp_path):
    path = tmp_path / "private" / "control.sock"
    edges = []
    with ControlServer(lambda k: edges.append(("down", k)), lambda k: edges.append(("up", k)),
                       {"ctrl_r"}, {"alt_r"}, lambda: edges.append(("exit", "")), path=path) as server:
        for action in ["ping", "ptt-press", "ptt-release", "toggle-press", "toggle-release"]:
            worker = threading.Thread(target=server.poll)
            worker.start()
            assert send_action(action, path=path) in {"ok", "ready"}
            worker.join(2)
            assert not worker.is_alive()
        assert path.stat().st_mode & 0o777 == 0o600
        assert server.dispatch("unknown") == "error: unknown action"
    assert edges == [("down", "ctrl_r"), ("up", "ctrl_r"), ("down", "alt_r"), ("up", "alt_r")]
    assert not path.exists()


def test_refuses_regular_file_and_public_directory(tmp_path):
    path = tmp_path / "control.sock"
    tmp_path.chmod(0o700)
    path.write_text("keep me")
    server = ControlServer(None, None, set(), set(), None, path=path)
    with pytest.raises(RuntimeError, match="Unexpected file"):
        server.__enter__()
    assert path.read_text() == "keep me"
    tmp_path.chmod(0o755)
    with pytest.raises(RuntimeError, match="0700"):
        server.__enter__()


def test_fragmented_command_waits_for_newline(tmp_path):
    events = []
    path = tmp_path / "private" / "control.sock"
    with ControlServer(events.append, events.append, {"ctrl_r"}, set(), None, path=path) as server:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
            client.sendall(b"ptt-")
            worker = threading.Thread(target=server.poll)
            worker.start()
            client.sendall(b"press\n")
            assert client.recv(256) == b"ok\n"
            worker.join(2)
    assert events == ["ctrl_r"]


def test_atomic_toggle_releases_and_status_is_read_only(tmp_path):
    edges = []
    server = ControlServer(lambda k: edges.append(("down", k)), lambda k: edges.append(("up", k)),
                           {"ctrl_r"}, {"alt_r"}, None, path=tmp_path / "sock",
                           get_status=lambda: "recording")
    assert server.dispatch("status") == "recording"
    assert edges == []
    assert server.dispatch("toggle") == "ok"
    assert server.dispatch("toggle") == "ok"
    assert edges == [("down", "alt_r"), ("up", "alt_r")] * 2


def test_status_socket_reply(tmp_path):
    path = tmp_path / "private" / "control.sock"
    with ControlServer(None, None, set(), set(), None, path=path,
                       get_status=lambda: "transcribing") as server:
        worker = threading.Thread(target=server.poll)
        worker.start()
        assert send_action("status", path=path) == "transcribing"
        worker.join(2)
