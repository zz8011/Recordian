"""测试 BackendManager 的进程管理和异常处理"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from recordian.backend_manager import (
    BackendManager,
    _terminate_backend_process,
    parse_backend_event_line,
)


@pytest.fixture(autouse=True)
def isolated_lifelines():
    """Mocked Popen/threads must not leave real pipe endpoints open."""
    with patch("recordian.backend_manager._BACKEND_LIFELINES", {}) as lifelines:
        yield
        for endpoint in lifelines.values():
            endpoint.close()


def test_old_wait_cannot_close_reused_fd_of_new_backend():
    from recordian.backend_lifecycle import ParentEndpoint
    from recordian.backend_manager import _BACKEND_LIFELINES, _release_backend_lifeline

    old_proc, new_proc = Mock(), Mock()
    old_fd = os.open(os.devnull, os.O_WRONLY)
    old_endpoint = ParentEndpoint(old_fd)
    _BACKEND_LIFELINES[old_proc] = old_endpoint
    _release_backend_lifeline(old_proc)
    new_fd = os.open(os.devnull, os.O_WRONLY)
    assert new_fd == old_fd, "fixture must force descriptor number reuse"
    _BACKEND_LIFELINES[new_proc] = ParentEndpoint(new_fd)
    manager = BackendManager(Path("/tmp/test_config.json"), queue.Queue(), Mock(), Mock())
    manager.proc = new_proc
    manager._wait(old_proc)
    old_endpoint.close()
    os.fstat(new_fd)  # Neither the old wait nor repeated close may close this fd.
    assert new_proc in _BACKEND_LIFELINES


def test_spawn_failure_closes_both_pipe_ends():
    from recordian.backend_lifecycle import spawn_backend

    pipe = os.pipe
    fds = []

    def capture_pipe():
        pair = pipe()
        fds.extend(pair)
        return pair

    with (
        patch("recordian.backend_lifecycle.os.pipe", side_effect=capture_pipe),
        patch("recordian.backend_lifecycle.subprocess.Popen", side_effect=OSError("spawn failed")),
        pytest.raises(OSError, match="spawn failed"),
    ):
        spawn_backend(["unused"])
    assert len(fds) == 2
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_exit_cleanup_closes_lifeline_even_when_termination_raises():
    from recordian.backend_lifecycle import ParentEndpoint
    from recordian.backend_manager import _BACKEND_LIFELINES, _cleanup_backend_processes

    proc = Mock()
    proc.poll.return_value = None
    fd = os.open(os.devnull, os.O_WRONLY)
    _BACKEND_LIFELINES[proc] = ParentEndpoint(fd)
    with (
        patch("recordian.backend_manager._ACTIVE_BACKEND_PROCESSES", [proc]) as processes,
        patch("recordian.backend_manager._terminate_backend_process", side_effect=RuntimeError("stop failed")),
        pytest.raises(RuntimeError, match="stop failed"),
    ):
        _cleanup_backend_processes()
    assert processes == []
    assert proc not in _BACKEND_LIFELINES
    with pytest.raises(OSError):
        os.fstat(fd)


class TestParseBackendEventLine:
    """测试事件解析函数"""

    def test_parse_valid_json_event(self) -> None:
        """测试解析有效的 JSON 事件"""
        line = '{"event": "ready", "data": "test"}'
        result = parse_backend_event_line(line)
        assert result is not None
        assert result["event"] == "ready"
        assert result["data"] == "test"

    def test_parse_empty_line(self) -> None:
        """测试解析空行"""
        result = parse_backend_event_line("")
        assert result is None

        result = parse_backend_event_line("   \n")
        assert result is None

    def test_parse_invalid_json(self) -> None:
        """测试解析无效 JSON"""
        result = parse_backend_event_line("not a json")
        assert result is None

        result = parse_backend_event_line('{"incomplete": ')
        assert result is None

    def test_parse_json_without_event_key(self) -> None:
        """测试解析没有 event 键的 JSON"""
        result = parse_backend_event_line('{"data": "test"}')
        assert result is None

    def test_parse_non_dict_json(self) -> None:
        """测试解析非字典的 JSON"""
        result = parse_backend_event_line('["array"]')
        assert result is None

        result = parse_backend_event_line('"string"')
        assert result is None


class TestBackendManagerInit:
    """测试 BackendManager 初始化"""

    def test_init_creates_manager(self) -> None:
        """测试初始化创建管理器"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        assert manager.config_path == config_path
        assert manager.proc is None
        assert manager._threads == []


class TestBackendManagerStart:
    """测试后端进程启动"""

    @pytest.mark.parametrize(
        "command",
        [
            "python -m recordian.hotkey_dictate --config-path /tmp/other-config.json",
            "/other-worktree/.venv/bin/python -m recordian.hotkey_dictate "
            "--config-path /tmp/test_config.json",
            "python -c 'print(\"recordian.hotkey_dictate\")'",
            "python unrelated.py recordian.hotkey_dictate",
            "python -m recordian.hotkey_dictate --config-path /tmp/test_config.json",
            "/usr/bin/ffmpeg -f pulse -i default /tmp/recordian-ptt-a/input.ogg pipe:1",
        ],
        ids=[
            "other-config",
            "other-worktree-same-config",
            "python-code-string",
            "unrelated-script-argument",
            "same-config-without-owner-evidence",
            "recorder-without-config-evidence",
        ],
    )
    def test_start_does_not_signal_processes_without_ownership(self, command: str) -> None:
        """Starting a backend must not adopt processes found by command text."""
        manager = BackendManager(
            config_path=Path("/tmp/test_config.json"),
            events=queue.Queue(),
            on_state_change=Mock(),
            on_menu_update=Mock(),
        )
        new_proc = Mock()
        new_proc.poll.return_value = None
        with (
            patch("recordian.backend_manager._ACTIVE_BACKEND_PROCESSES", []),
            patch("recordian.backend_manager.subprocess.Popen", return_value=new_proc),
            patch("recordian.backend_manager.threading.Thread"),
            patch("recordian.backend_manager.subprocess.run") as run,
            patch("recordian.backend_manager.os.kill") as kill,
            patch("recordian.backend_manager.os.killpg") as killpg,
            patch("time.monotonic", side_effect=[0.0, 2.0]),
        ):
            run.return_value = Mock(stdout=f"900001 {command}\n")

            manager.start()

            assert kill.call_args_list == []
            assert killpg.call_args_list == []
            assert run.call_args_list == []
            assert manager.proc is new_proc
            assert manager._events.empty()

    @patch("recordian.backend_manager.subprocess.Popen")
    def test_start_launches_subprocess(self, mock_popen: Mock) -> None:
        """测试启动时创建子进程"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = Mock()
        mock_proc.stderr = Mock()
        mock_proc.stdout.readline.side_effect = [""]
        mock_proc.stderr.readline.side_effect = [""]
        mock_popen.return_value = mock_proc

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        manager.start()

        # 验证子进程被启动
        mock_popen.assert_called_once()
        assert mock_popen.call_args.kwargs["start_new_session"] is True
        assert manager.proc == mock_proc
        on_state_change.assert_called_once_with(True, "starting", "Starting backend...")
        on_menu_update.assert_called_once()

    @patch("recordian.backend_manager.subprocess.Popen")
    def test_start_does_not_restart_running_process(self, mock_popen: Mock) -> None:
        """测试不会重复启动已运行的进程"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        manager.proc = mock_proc
        manager.start()

        # 验证没有创建新进程
        mock_popen.assert_not_called()



class TestBackendManagerStop:
    """测试后端进程停止"""

    @patch("recordian.backend_manager._terminate_backend_process")
    def test_stop_terminates_process(self, mock_terminate_backend_process: Mock) -> None:
        """测试停止时终止进程"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        manager.stop()

        # 验证进程被终止
        mock_terminate_backend_process.assert_called_once_with(mock_proc)
        assert manager.proc is None

        # 验证事件被发送
        event = events.get_nowait()
        assert event["event"] == "stopped"

    @patch("recordian.backend_manager._terminate_backend_process")
    def test_stop_ignores_already_exited_process(self, mock_terminate_backend_process: Mock) -> None:
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = 0

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        manager.stop()

        mock_terminate_backend_process.assert_not_called()

    def test_stop_does_nothing_when_no_process(self) -> None:
        """测试没有进程时停止不报错"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        # 不应该抛出异常
        manager.stop()
        assert manager.proc is None


class TestBackendManagerStreamReading:
    """测试 stdout/stderr 流读取"""

    def test_read_stream_parses_events(self) -> None:
        """测试读取流并解析事件"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        # 模拟 stdout 流
        mock_stream = Mock()
        mock_stream.readline.side_effect = [
            '{"event": "ready"}\n',
            '{"event": "recording"}\n',
            "",  # EOF
        ]

        manager._read_stream(mock_stream, is_stderr=False)

        # 验证事件被解析
        event1 = events.get_nowait()
        assert event1["event"] == "ready"

        event2 = events.get_nowait()
        assert event2["event"] == "recording"

        mock_stream.close.assert_called_once()

    def test_read_stream_handles_stderr_logs(self) -> None:
        """测试 stderr 流生成日志事件"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        # 模拟 stderr 流
        mock_stream = Mock()
        mock_stream.readline.side_effect = [
            "Error: something went wrong\n",
            "Warning: deprecated API\n",
            "",  # EOF
        ]

        manager._read_stream(mock_stream, is_stderr=True)

        # 验证日志事件被生成
        event1 = events.get_nowait()
        assert event1["event"] == "log"
        assert event1["message"] == "Error: something went wrong"

        event2 = events.get_nowait()
        assert event2["event"] == "log"
        assert event2["message"] == "Warning: deprecated API"

    def test_read_stream_ignores_invalid_json(self) -> None:
        """测试忽略无效 JSON 行"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )

        # 模拟混合流
        mock_stream = Mock()
        mock_stream.readline.side_effect = [
            '{"event": "ready"}\n',
            "invalid json line\n",
            '{"event": "done"}\n',
            "",  # EOF
        ]

        manager._read_stream(mock_stream, is_stderr=False)

        # 验证只有有效事件被解析
        event1 = events.get_nowait()
        assert event1["event"] == "ready"

        event2 = events.get_nowait()
        assert event2["event"] == "done"

        # 队列应该为空
        assert events.empty()


class TestBackendManagerProcessExit:
    """测试子进程异常退出"""

    def test_wait_sends_exit_event(self) -> None:
        """测试进程退出时发送事件"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.wait.return_value = 1

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        manager._wait()

        # 验证退出事件被发送
        event = events.get_nowait()
        assert event["event"] == "backend_exited"
        assert event["code"] == 1

    def test_wait_handles_none_process(self) -> None:
        """测试 proc 为 None 时不报错"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = None

        # 不应该抛出异常
        manager._wait()
        assert events.empty()


class TestBackendManagerRestart:
    """测试重启功能"""

    @patch("recordian.backend_manager._terminate_backend_process")
    @patch("recordian.backend_manager.subprocess.Popen")
    def test_restart_stops_and_starts(
        self,
        mock_popen: Mock,
        mock_terminate_backend_process: Mock,
    ) -> None:
        """测试重启先停止后启动"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0
        mock_proc.stdout = Mock()
        mock_proc.stderr = Mock()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        # 配置 mock 以便重启后返回新进程
        new_proc = Mock()
        new_proc.poll.return_value = None
        new_proc.stdout = Mock()
        new_proc.stderr = Mock()
        new_proc.stdout.readline.side_effect = [""]
        new_proc.stderr.readline.side_effect = [""]
        mock_popen.return_value = new_proc

        manager.restart()

        # 验证旧进程被终止
        mock_terminate_backend_process.assert_called_once_with(mock_proc)

        # 验证新进程被启动
        mock_popen.assert_called_once()
        assert manager.proc == new_proc


class TestBackendManagerCleanup:
    """测试进程清理功能"""

    @patch("recordian.backend_manager._terminate_backend_process")
    def test_stop_handles_terminate_helper_exceptions(self, mock_terminate_backend_process: Mock) -> None:
        """测试停止时清理 helper 抛错会继续抛出前不会污染状态"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_terminate_backend_process.side_effect = RuntimeError("boom")

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        with patch("recordian.backend_manager._ACTIVE_BACKEND_PROCESSES", [mock_proc]):
            try:
                manager.stop()
            except RuntimeError:
                pass
        assert manager.proc == mock_proc

    def test_stop_removes_from_registry(self) -> None:
        """测试停止时从全局注册表移除进程"""
        from recordian.backend_manager import _ACTIVE_BACKEND_PROCESSES

        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        # 手动添加到注册表
        _ACTIVE_BACKEND_PROCESSES.append(mock_proc)

        with patch("recordian.backend_manager._terminate_backend_process") as mock_terminate_backend_process:
            manager.stop()
        mock_terminate_backend_process.assert_called_once_with(mock_proc)

        # 验证从注册表移除
        assert mock_proc not in _ACTIVE_BACKEND_PROCESSES

    def test_cleanup_backend_processes(self) -> None:
        """测试全局清理函数"""
        from recordian.backend_manager import _ACTIVE_BACKEND_PROCESSES, _cleanup_backend_processes

        # 创建模拟进程
        mock_proc1 = Mock()
        mock_proc1.poll.return_value = None
        mock_proc1.wait.return_value = 0

        mock_proc2 = Mock()
        mock_proc2.poll.return_value = 1  # 已退出

        # 添加到注册表
        _ACTIVE_BACKEND_PROCESSES.clear()
        _ACTIVE_BACKEND_PROCESSES.append(mock_proc1)
        _ACTIVE_BACKEND_PROCESSES.append(mock_proc2)

        # 执行清理
        with patch("recordian.backend_manager._terminate_backend_process") as mock_terminate_backend_process:
            _cleanup_backend_processes()

        # 验证运行中的进程被终止
        mock_terminate_backend_process.assert_called_once_with(mock_proc1)

        # 验证已退出的进程不被终止
        assert mock_terminate_backend_process.call_count == 1

        # 验证注册表被清空
        assert len(_ACTIVE_BACKEND_PROCESSES) == 0

    @patch("recordian.backend_manager.os.killpg")
    @patch("recordian.backend_manager.os.getpgid")
    def test_terminate_backend_process_uses_process_group(
        self,
        mock_getpgid: Mock,
        mock_killpg: Mock,
    ) -> None:
        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 4321
        mock_proc.wait.return_value = 0

        mock_getpgid.return_value = 4321

        _terminate_backend_process(mock_proc)

        mock_killpg.assert_called_once_with(4321, signal.SIGTERM)
        mock_proc.terminate.assert_not_called()
        mock_proc.wait.assert_called_once_with(timeout=2.0)

    @patch("recordian.backend_manager.os.killpg")
    @patch("recordian.backend_manager.os.getpgid")
    def test_terminate_backend_process_falls_back_to_sigkill_after_timeout(
        self,
        mock_getpgid: Mock,
        mock_killpg: Mock,
    ) -> None:
        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 4321
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired("cmd", 2.0),
            0,
        ]

        mock_getpgid.return_value = 4321

        _terminate_backend_process(mock_proc)

        assert mock_killpg.call_args_list == [
            ((4321, signal.SIGTERM),),
            ((4321, signal.SIGKILL),),
        ]
        assert mock_proc.wait.call_count == 2

    @patch("recordian.backend_manager.os.killpg")
    @patch("recordian.backend_manager.os.getpgid", return_value=9876)
    def test_terminate_does_not_signal_a_different_process_group(self, getpgid, killpg):
        proc = Mock(pid=4321)
        proc.poll.return_value = None
        _terminate_backend_process(proc)
        killpg.assert_not_called()
        proc.terminate.assert_called_once()

    @patch("recordian.backend_manager.os.killpg")
    @patch("recordian.backend_manager.os.getpgid", return_value=4321)
    def test_terminate_does_not_signal_a_child_reaped_during_group_lookup(self, getpgid, killpg):
        proc = Mock(pid=4321)
        proc.poll.side_effect = [None, 0]
        _terminate_backend_process(proc)
        killpg.assert_not_called()
        proc.terminate.assert_not_called()



class TestBackendManagerRequestStopRecording:
    """测试 overlay 点击停止录音的 SIGUSR1 通道"""

    @patch("recordian.backend_manager.os.kill")
    def test_sends_sigusr1_to_backend_process_only(self, mock_kill: Mock) -> None:
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=Mock(),
            on_menu_update=Mock(),
        )
        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 4321
        manager.proc = mock_proc

        assert manager.request_stop_recording() is True
        mock_kill.assert_called_once_with(4321, signal.SIGUSR1)

    @patch("recordian.backend_manager.os.kill")
    def test_returns_false_when_backend_not_running(self, mock_kill: Mock) -> None:
        manager = BackendManager(
            config_path=Path("/tmp/test_config.json"),
            events=queue.Queue(),
            on_state_change=Mock(),
            on_menu_update=Mock(),
        )
        assert manager.request_stop_recording() is False
        mock_kill.assert_not_called()

        mock_proc = Mock()
        mock_proc.poll.return_value = 0  # 已退出
        manager.proc = mock_proc
        assert manager.request_stop_recording() is False
        mock_kill.assert_not_called()

    @patch("recordian.backend_manager.os.kill")
    def test_returns_false_on_oserror(self, mock_kill: Mock) -> None:
        manager = BackendManager(
            config_path=Path("/tmp/test_config.json"),
            events=queue.Queue(),
            on_state_change=Mock(),
            on_menu_update=Mock(),
        )
        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 4321
        manager.proc = mock_proc
        mock_kill.side_effect = ProcessLookupError()

        assert manager.request_stop_recording() is False
