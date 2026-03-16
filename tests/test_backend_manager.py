"""测试 BackendManager 的进程管理和异常处理"""
from __future__ import annotations

import queue
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from recordian.backend_manager import (
    BackendManager,
    _cleanup_orphan_recordian_recorders,
    _is_recordian_recorder_command,
    _list_orphan_recordian_recorder_pids,
    _terminate_backend_process,
    parse_backend_event_line,
)


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

    @patch("recordian.backend_manager._cleanup_orphan_recordian_recorders")
    @patch("recordian.backend_manager.subprocess.Popen")
    def test_start_launches_subprocess(self, mock_popen: Mock, mock_cleanup_orphans: Mock) -> None:
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
        mock_cleanup_orphans.assert_called_once()

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

    @patch("recordian.backend_manager._cleanup_orphan_recordian_recorders")
    @patch("recordian.backend_manager.subprocess.Popen")
    def test_start_logs_orphan_cleanup(self, mock_popen: Mock, mock_cleanup_orphans: Mock) -> None:
        config_path = Path("/tmp/test_config.json")
        events = queue.Queue()
        on_state_change = Mock()
        on_menu_update = Mock()

        mock_cleanup_orphans.return_value = 3
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

        assert events.get_nowait() == {"event": "log", "message": "cleaned_orphan_recorders:3"}


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

    @patch("recordian.backend_manager._cleanup_orphan_recordian_recorders")
    @patch("recordian.backend_manager._terminate_backend_process")
    @patch("recordian.backend_manager.subprocess.Popen")
    def test_restart_stops_and_starts(
        self,
        mock_popen: Mock,
        mock_terminate_backend_process: Mock,
        mock_cleanup_orphans: Mock,
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
        mock_cleanup_orphans.assert_called_once()


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

    @patch("recordian.backend_manager.subprocess.run")
    def test_list_orphan_recordian_recorder_pids_filters_recordian_ffmpeg(self, mock_run: Mock) -> None:
        mock_run.return_value = Mock(
            stdout=(
                "100 /usr/bin/ffmpeg -hide_banner -loglevel error -y -f pulse -i default "
                "-ac 1 -ar 16000 -filter_complex [0:a]asplit=2[record][monitor] "
                "-map [record] -c:a libopus -b:a 24k /tmp/recordian-ptt-a/input.ogg "
                "-map [monitor] -f f32le -acodec pcm_f32le pipe:1\n"
                "101 /usr/bin/ffmpeg -hide_banner -loglevel error -y -f x11grab -i :0 pipe:1\n"
            )
        )

        result = _list_orphan_recordian_recorder_pids(exclude_pids={100})

        assert result == []

        result = _list_orphan_recordian_recorder_pids()
        assert result == [100]

    def test_is_recordian_recorder_command_matches_expected_ffmpeg(self) -> None:
        command = (
            "/usr/bin/ffmpeg -hide_banner -loglevel error -y -f pulse -i default "
            "-ac 1 -ar 16000 -filter_complex [0:a]asplit=2[record][monitor] "
            "-map [record] -c:a libopus -b:a 24k /tmp/recordian-ptt-a/input.ogg "
            "-map [monitor] -f f32le -acodec pcm_f32le pipe:1"
        )
        assert _is_recordian_recorder_command(command) is True
        assert _is_recordian_recorder_command("/usr/bin/ffmpeg -f x11grab -i :0 pipe:1") is False

    @patch("recordian.backend_manager.time.sleep")
    @patch("recordian.backend_manager.time.monotonic")
    @patch("recordian.backend_manager.os.kill")
    @patch("recordian.backend_manager._list_orphan_recordian_recorder_pids")
    def test_cleanup_orphan_recordian_recorders_terminates_and_kills_survivors(
        self,
        mock_list_pids: Mock,
        mock_kill: Mock,
        mock_monotonic: Mock,
        mock_sleep: Mock,
    ) -> None:
        mock_list_pids.return_value = [101, 102]
        mock_monotonic.side_effect = [0.0, 0.2, 2.0]

        def _kill_side_effect(pid: int, sig: int) -> None:
            if sig == 0 and pid == 101:
                raise ProcessLookupError()

        mock_kill.side_effect = _kill_side_effect

        cleaned = _cleanup_orphan_recordian_recorders()

        assert cleaned == 2
        assert mock_kill.call_args_list == [
            ((101, signal.SIGTERM),),
            ((102, signal.SIGTERM),),
            ((101, 0),),
            ((102, 0),),
            ((102, signal.SIGKILL),),
        ]
        mock_sleep.assert_called_once()
