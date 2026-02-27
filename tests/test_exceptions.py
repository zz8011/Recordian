"""测试 Recordian 异常层次结构"""
from __future__ import annotations

import pytest

from recordian.exceptions import (
    ASRError,
    AudioError,
    BackendError,
    CommitError,
    ConfigError,
    RecordianError,
    RefinerError,
    ResourceError,
    TimeoutError,
)


class TestExceptionHierarchy:
    """测试异常继承关系"""

    def test_all_exceptions_inherit_from_recordian_error(self) -> None:
        """测试所有自定义异常都继承自 RecordianError"""
        exceptions = [
            ASRError,
            AudioError,
            BackendError,
            CommitError,
            ConfigError,
            RefinerError,
            ResourceError,
            TimeoutError,
        ]

        for exc_class in exceptions:
            assert issubclass(exc_class, RecordianError)
            assert issubclass(exc_class, Exception)

    def test_recordian_error_is_exception(self) -> None:
        """测试 RecordianError 继承自 Exception"""
        assert issubclass(RecordianError, Exception)

    def test_exceptions_can_be_raised_and_caught(self) -> None:
        """测试异常可以正常抛出和捕获"""
        with pytest.raises(ASRError):
            raise ASRError("ASR failed")

        with pytest.raises(CommitError):
            raise CommitError("Commit failed")

        with pytest.raises(ConfigError):
            raise ConfigError("Config invalid")

    def test_catch_specific_exception(self) -> None:
        """测试可以捕获特定异常"""
        try:
            raise ASRError("test error")
        except ASRError as e:
            assert str(e) == "test error"
        except RecordianError:
            pytest.fail("Should catch ASRError, not RecordianError")

    def test_catch_base_exception(self) -> None:
        """测试可以用基类捕获所有自定义异常"""
        exceptions_raised = []

        for exc_class in [ASRError, CommitError, ConfigError]:
            try:
                raise exc_class("test")
            except RecordianError:
                exceptions_raised.append(exc_class)

        assert len(exceptions_raised) == 3

    def test_exception_with_context(self) -> None:
        """测试异常可以携带上下文信息"""
        context = {"file": "test.wav", "line": 42}
        error_msg = f"Failed to process: {context}"

        with pytest.raises(AudioError) as exc_info:
            raise AudioError(error_msg)

        assert "test.wav" in str(exc_info.value)
        assert "42" in str(exc_info.value)


class TestExceptionUsage:
    """测试异常使用场景"""

    def test_asr_error_usage(self) -> None:
        """测试 ASRError 使用场景"""
        with pytest.raises(ASRError):
            raise ASRError("Speech recognition failed")

    def test_refiner_error_usage(self) -> None:
        """测试 RefinerError 使用场景"""
        with pytest.raises(RefinerError):
            raise RefinerError("LLM inference timeout")

    def test_commit_error_usage(self) -> None:
        """测试 CommitError 使用场景"""
        with pytest.raises(CommitError):
            raise CommitError("Clipboard operation failed")

    def test_config_error_usage(self) -> None:
        """测试 ConfigError 使用场景"""
        with pytest.raises(ConfigError):
            raise ConfigError("Invalid configuration value")

    def test_backend_error_usage(self) -> None:
        """测试 BackendError 使用场景"""
        with pytest.raises(BackendError):
            raise BackendError("Backend process crashed")

    def test_audio_error_usage(self) -> None:
        """测试 AudioError 使用场景"""
        with pytest.raises(AudioError):
            raise AudioError("Audio device not available")

    def test_timeout_error_usage(self) -> None:
        """测试 TimeoutError 使用场景"""
        with pytest.raises(TimeoutError):
            raise TimeoutError("Operation timed out")

    def test_resource_error_usage(self) -> None:
        """测试 ResourceError 使用场景"""
        with pytest.raises(ResourceError):
            raise ResourceError("Disk space insufficient")
