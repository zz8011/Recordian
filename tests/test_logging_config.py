"""测试 Recordian 日志配置"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from recordian.logging_config import (
    configure_from_env,
    get_logger,
    set_level,
    setup_logging,
)


class TestLoggingSetup:
    """测试日志系统配置"""

    def test_setup_logging_creates_logger(self, tmp_path: Path) -> None:
        """测试 setup_logging 创建 logger"""
        log_file = tmp_path / "test.log"
        logger = setup_logging(log_file=log_file, console=False)

        assert logger is not None
        assert logger.name == "recordian"
        assert logger.level == logging.INFO

    def test_setup_logging_creates_log_file(self, tmp_path: Path) -> None:
        """测试日志文件创建"""
        log_file = tmp_path / "test.log"
        logger = setup_logging(log_file=log_file, console=False, force_reconfigure=True)

        # 写入日志
        logger.info("Test message")

        # 验证文件存在
        assert log_file.exists()
        content = log_file.read_text()
        assert "Test message" in content

    def test_setup_logging_with_custom_level(self, tmp_path: Path) -> None:
        """测试自定义日志级别"""
        log_file = tmp_path / "test.log"
        logger = setup_logging(level=logging.DEBUG, log_file=log_file, console=False, force_reconfigure=True)

        assert logger.level == logging.DEBUG

        # DEBUG 消息应该被记录
        logger.debug("Debug message")
        content = log_file.read_text()
        assert "Debug message" in content

    def test_setup_logging_idempotent(self, tmp_path: Path) -> None:
        """测试重复调用 setup_logging 是幂等的"""
        log_file = tmp_path / "test.log"
        logger1 = setup_logging(log_file=log_file, console=False)
        logger2 = setup_logging(log_file=log_file, console=False)

        assert logger1 is logger2
        # 不应该重复添加 handler
        assert len(logger1.handlers) == len(logger2.handlers)

    def test_log_rotation_config(self, tmp_path: Path) -> None:
        """测试日志轮转配置"""
        log_file = tmp_path / "test.log"
        max_bytes = 1024
        backup_count = 3

        logger = setup_logging(
            log_file=log_file,
            console=False,
            max_bytes=max_bytes,
            backup_count=backup_count,
            force_reconfigure=True,
        )

        # 验证 RotatingFileHandler 配置
        from logging.handlers import RotatingFileHandler

        handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1

        handler = handlers[0]
        assert handler.maxBytes == max_bytes
        assert handler.backupCount == backup_count


class TestGetLogger:
    """测试 get_logger 函数"""

    def test_get_logger_without_name(self, tmp_path: Path) -> None:
        """测试获取默认 logger"""
        setup_logging(log_file=tmp_path / "test.log", console=False, force_reconfigure=True)
        logger = get_logger()

        assert logger.name == "recordian"

    def test_get_logger_with_name(self, tmp_path: Path) -> None:
        """测试获取命名 logger"""
        setup_logging(log_file=tmp_path / "test.log", console=False, force_reconfigure=True)
        logger = get_logger("module")

        assert logger.name == "recordian.module"

    def test_child_logger_inherits_config(self, tmp_path: Path) -> None:
        """测试子 logger 继承配置"""
        log_file = tmp_path / "test.log"
        setup_logging(level=logging.DEBUG, log_file=log_file, console=False, force_reconfigure=True)

        child_logger = get_logger("child")
        child_logger.debug("Child debug message")

        content = log_file.read_text()
        assert "Child debug message" in content


class TestSetLevel:
    """测试动态设置日志级别"""

    def test_set_level_with_int(self, tmp_path: Path) -> None:
        """测试使用整数设置日志级别"""
        logger = setup_logging(log_file=tmp_path / "test.log", console=False, force_reconfigure=True)
        set_level(logging.WARNING)

        assert logger.level == logging.WARNING

    def test_set_level_with_string(self, tmp_path: Path) -> None:
        """测试使用字符串设置日志级别"""
        logger = setup_logging(log_file=tmp_path / "test.log", console=False, force_reconfigure=True)
        set_level("ERROR")

        assert logger.level == logging.ERROR

    def test_set_level_affects_handlers(self, tmp_path: Path) -> None:
        """测试设置级别影响所有 handler"""
        logger = setup_logging(log_file=tmp_path / "test.log", console=False, force_reconfigure=True)
        set_level(logging.WARNING)

        for handler in logger.handlers:
            assert handler.level == logging.WARNING


class TestConfigureFromEnv:
    """测试从环境变量配置"""

    def test_configure_from_env_default(self, tmp_path: Path, monkeypatch) -> None:
        """测试默认环境变量配置"""
        # 清除可能存在的环境变量
        monkeypatch.delenv("RECORDIAN_LOG_LEVEL", raising=False)
        monkeypatch.delenv("RECORDIAN_LOG_FILE", raising=False)

        # 清除现有 logger
        logger = logging.getLogger("recordian")
        logger.handlers.clear()

        logger = configure_from_env()

        assert logger.level == logging.INFO

    def test_configure_from_env_custom_level(self, tmp_path: Path, monkeypatch) -> None:
        """测试自定义日志级别"""
        monkeypatch.setenv("RECORDIAN_LOG_LEVEL", "DEBUG")

        # 清除现有 logger
        logger = logging.getLogger("recordian")
        logger.handlers.clear()

        logger = configure_from_env()

        assert logger.level == logging.DEBUG

    def test_configure_from_env_custom_file(self, tmp_path: Path, monkeypatch) -> None:
        """测试自定义日志文件"""
        log_file = tmp_path / "custom.log"
        monkeypatch.setenv("RECORDIAN_LOG_FILE", str(log_file))

        # 清除现有 logger
        logger = logging.getLogger("recordian")
        logger.handlers.clear()

        logger = configure_from_env()
        logger.info("Test message")

        assert log_file.exists()

    def test_configure_from_env_disable_console(self, tmp_path: Path, monkeypatch) -> None:
        """测试禁用控制台输出"""
        monkeypatch.setenv("RECORDIAN_LOG_CONSOLE", "0")

        # 清除现有 logger
        logger = logging.getLogger("recordian")
        logger.handlers.clear()

        logger = configure_from_env()

        # 验证没有 StreamHandler
        stream_handlers = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
        # RotatingFileHandler 也是 StreamHandler 的子类，所以只检查非文件的
        from logging.handlers import RotatingFileHandler
        non_file_stream_handlers = [
            h for h in stream_handlers if not isinstance(h, RotatingFileHandler)
        ]
        assert len(non_file_stream_handlers) == 0


class TestLogFormat:
    """测试日志格式"""

    def test_log_format_includes_timestamp(self, tmp_path: Path) -> None:
        """测试日志包含时间戳"""
        log_file = tmp_path / "test.log"
        logger = setup_logging(log_file=log_file, console=False, force_reconfigure=True)

        logger.info("Test message")

        content = log_file.read_text()
        # 验证包含时间戳格式 YYYY-MM-DD HH:MM:SS
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", content)

    def test_log_format_includes_level(self, tmp_path: Path) -> None:
        """测试日志包含级别"""
        log_file = tmp_path / "test.log"
        logger = setup_logging(log_file=log_file, console=False, force_reconfigure=True)

        logger.info("Info message")
        logger.warning("Warning message")

        content = log_file.read_text()
        assert "INFO" in content
        assert "WARNING" in content

    def test_log_format_includes_logger_name(self, tmp_path: Path) -> None:
        """测试日志包含 logger 名称"""
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file, console=False, force_reconfigure=True)

        logger = get_logger("test_module")
        logger.info("Test message")

        content = log_file.read_text()
        assert "recordian.test_module" in content
