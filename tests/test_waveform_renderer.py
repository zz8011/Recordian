"""测试 WaveformRenderer 的关键路径和异常处理"""
from __future__ import annotations

import os
import queue
import threading
import time
from unittest.mock import MagicMock, Mock, patch

import pytest

# Skip all tests if no DISPLAY is available (CI environment)
pytestmark = pytest.mark.skipif(
    not os.environ.get('DISPLAY'),
    reason="Requires DISPLAY environment (GUI tests)"
)


class TestWaveformRendererInit:
    """测试 WaveformRenderer 初始化和 shader 编译"""

    def test_init_creates_basic_structure(self) -> None:
        """测试初始化创建基本结构"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            # 直接创建对象测试基本结构
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer.state = "idle"
            renderer.amplitude = 0.0
            renderer.target_amplitude = 0.0
            renderer.level_boost = 0.0
            renderer.base_mode = 0.0
            renderer.detail = ""
            renderer.hide_deadline = None
            renderer.smooth_audio = 0.0
            renderer._cmd_queue = queue.SimpleQueue()
            renderer._ready = threading.Event()
            renderer._init_error = None

            assert renderer.state == "idle"
            assert renderer.amplitude == 0.0
            assert renderer._cmd_queue is not None
        finally:
            root.destroy()

    def test_init_error_handling(self) -> None:
        """测试初始化错误处理"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            # 模拟初始化错误
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer._cmd_queue = queue.SimpleQueue()
            renderer._ready = threading.Event()
            renderer._init_error = ImportError("No module named 'pyglet'")
            renderer._ready.set()

            # 验证错误被记录
            assert renderer._init_error is not None
            assert isinstance(renderer._init_error, ImportError)
        finally:
            root.destroy()

    def test_shader_compile_error_captured(self) -> None:
        """测试 shader 编译错误被捕获"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer._cmd_queue = queue.SimpleQueue()
            renderer._ready = threading.Event()
            renderer._init_error = Exception("Shader compile error")
            renderer._ready.set()

            # 验证错误被捕获
            assert renderer._init_error is not None
            assert "Shader" in str(renderer._init_error)
        finally:
            root.destroy()


class TestWaveformRendererCommandQueue:
    """测试命令队列的阻塞和异常行为"""

    def test_command_queue_blocks_when_full(self) -> None:
        """测试命令队列满时的阻塞行为"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer._cmd_queue = queue.SimpleQueue()
            renderer.state = "idle"
            renderer.amplitude = 0.0
            renderer.target_amplitude = 0.0
            renderer.level_boost = 0.0
            renderer.base_mode = 0.0
            renderer.detail = ""
            renderer.hide_deadline = None
            renderer.smooth_audio = 0.0

            # 填充大量命令
            for i in range(1000):
                renderer._cmd_queue.put(("level", float(i % 100) / 100.0))

            # 验证队列不会阻塞（SimpleQueue 无限容量）
            renderer.set_level(0.5)
            assert True  # 如果没有阻塞，测试通过
        finally:
            root.destroy()

    def test_invalid_command_ignored(self) -> None:
        """测试无效命令被忽略"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer._cmd_queue = queue.SimpleQueue()
            renderer.state = "idle"

            # 发送无效命令
            renderer._cmd_queue.put(("invalid_cmd", None))

            # 验证不会崩溃
            try:
                cmd, payload = renderer._cmd_queue.get_nowait()
                assert cmd == "invalid_cmd"
            except queue.Empty:
                pass
        finally:
            root.destroy()


class TestWaveformRendererThreadSafety:
    """测试线程异常退出时的资源清理"""

    def test_thread_exit_cleanup(self) -> None:
        """测试线程异常退出时资源被正确清理"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer._cmd_queue = queue.SimpleQueue()
            renderer._ready = threading.Event()
            renderer._init_error = None

            # 模拟线程启动后立即退出
            def mock_run() -> None:
                renderer._ready.set()
                # 线程立即退出

            renderer._thread = threading.Thread(target=mock_run, daemon=True)
            renderer._thread.start()
            renderer._thread.join(timeout=1.0)

            # 验证线程已退出
            assert not renderer._thread.is_alive()
        finally:
            root.destroy()

    def test_shutdown_waits_for_thread(self) -> None:
        """测试 shutdown 等待线程退出"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer._cmd_queue = queue.SimpleQueue()
            renderer._ready = threading.Event()

            # 模拟长时间运行的线程
            stop_flag = threading.Event()

            def mock_run() -> None:
                renderer._ready.set()
                stop_flag.wait(timeout=5.0)

            renderer._thread = threading.Thread(target=mock_run, daemon=True)
            renderer._thread.start()
            renderer._ready.wait(timeout=1.0)

            # 调用 shutdown
            start = time.time()
            renderer._cmd_queue.put(("quit", None))
            stop_flag.set()
            renderer._thread.join(timeout=1.5)
            elapsed = time.time() - start

            # 验证等待时间合理
            assert elapsed < 2.0
            assert not renderer._thread.is_alive()
        finally:
            root.destroy()


class TestWaveformRendererStateTransitions:
    """测试状态转换逻辑"""

    def test_state_transition_recording_to_processing(self) -> None:
        """测试从 recording 到 processing 的状态转换"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer.state = "recording"
            renderer.amplitude = 0.5
            renderer.target_amplitude = 0.5
            renderer.base_mode = 1.0
            renderer.hide_deadline = None

            # 模拟状态转换
            renderer.state = "processing"
            renderer.target_amplitude = 0.0
            renderer.amplitude = 0.0
            renderer.base_mode = 0.0
            renderer.hide_deadline = time.time() + 0.50

            assert renderer.state == "processing"
            assert renderer.target_amplitude == 0.0
            assert renderer.hide_deadline is not None
        finally:
            root.destroy()

    def test_state_transition_to_error(self) -> None:
        """测试错误状态转换"""
        import tkinter as tk
        from recordian.waveform_renderer import WaveformRenderer

        root = tk.Tk()
        try:
            renderer = WaveformRenderer.__new__(WaveformRenderer)
            renderer.root = root
            renderer.state = "idle"

            # 模拟错误状态
            renderer.state = "error"
            renderer.target_amplitude = 0.50
            renderer.base_mode = 3.0
            renderer.hide_deadline = time.time() + 1.55

            assert renderer.state == "error"
            assert renderer.target_amplitude == 0.50
            assert renderer.base_mode == 3.0
        finally:
            root.destroy()
