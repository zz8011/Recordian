"""测试 BackendManager 的进程管理和异常处理"""
from __future__ import annotations

import queue
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from recordian.backend_manager import BackendManager, parse_backend_event_line


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

    def test_stop_terminates_process(self) -> None:
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
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called()
        assert manager.proc is None

        # 验证事件被发送
        event = events.get_nowait()
        assert event["event"] == "stopped"

    def test_stop_kills_process_on_timeout(self) -> None:
        """测试超时时强制杀死进程"""
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 2.0), 0]

        manager = BackendManager(
            config_path=config_path,
            events=events,
            on_state_change=on_state_change,
            on_menu_update=on_menu_update,
        )
        manager.proc = mock_proc

        manager.stop()

        # 验证先 terminate 后 kill
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert mock_proc.wait.call_count == 2

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

    @patch("recordian.backend_manager.subprocess.Popen")
    def test_restart_stops_and_starts(self, mock_popen: Mock) -> None:
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
        mock_popen.return_value = new_proc

        manager.restart()

        # 验证旧进程被终止
        mock_proc.terminate.assert_called_once()

        # 验证新进程被启动
        mock_popen.assert_called_once()
        assert manager.proc == new_proc
