"""ASR 识别集成测试"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from recordian.config import AppConfig
from recordian.engine import DictationEngine
from recordian.models import ASRResult, SessionState
from recordian.providers.base import ASRProvider


class MockASRProvider(ASRProvider):
    """Mock ASR Provider for testing"""

    def __init__(self, text: str = "测试文本", confidence: float = 0.95, is_cloud: bool = False) -> None:
        self.text = text
        self.confidence = confidence
        self._is_cloud = is_cloud
        self.call_count = 0

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str] | None = None) -> ASRResult:
        self.call_count += 1
        return ASRResult(
            text=self.text,
            confidence=self.confidence,
            model_name="mock-model",
        )

    @property
    def is_cloud(self) -> bool:
        return self._is_cloud


class TestASRIntegration:
    """测试 ASR 识别集成"""

    def test_single_pass_high_confidence(self, tmp_path: Path) -> None:
        """测试高置信度单次识别"""
        # 创建测试音频文件
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        # 创建 mock provider
        pass1_provider = MockASRProvider(text="这是高置信度文本", confidence=0.95)

        # 创建引擎
        engine = DictationEngine(pass1_provider)

        # 执行识别
        result = engine.transcribe_utterance(wav_path)

        # 验证结果
        assert result.state == SessionState.COMMIT
        assert result.text == "这是高置信度文本"
        assert result.pass1_result.confidence == 0.95
        assert result.pass2_result is None  # 高置信度不需要 pass2
        assert pass1_provider.call_count == 1

    def test_two_pass_low_confidence(self, tmp_path: Path) -> None:
        """测试低置信度双次识别"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        # 创建 mock providers
        pass1_provider = MockASRProvider(text="低置信度文本", confidence=0.75)
        pass2_provider = MockASRProvider(text="精炼后的文本", confidence=0.98, is_cloud=True)

        # 创建引擎
        engine = DictationEngine(pass1_provider, pass2_provider=pass2_provider)

        # 执行识别
        result = engine.transcribe_utterance(wav_path)

        # 验证结果
        assert result.state == SessionState.COMMIT
        assert result.text == "精炼后的文本"  # 使用 pass2 结果
        assert result.pass1_result.confidence == 0.75
        assert result.pass2_result is not None
        assert result.pass2_result.confidence == 0.98
        assert pass1_provider.call_count == 1
        assert pass2_provider.call_count == 1

    def test_force_high_precision(self, tmp_path: Path) -> None:
        """测试强制高精度模式"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        # 即使高置信度，也应该运行 pass2
        pass1_provider = MockASRProvider(text="原始文本", confidence=0.95)
        pass2_provider = MockASRProvider(text="强制精炼文本", confidence=0.98, is_cloud=True)

        engine = DictationEngine(pass1_provider, pass2_provider=pass2_provider)

        # 强制高精度
        result = engine.transcribe_utterance(wav_path, force_high_precision=True)

        # 验证 pass2 被调用
        assert result.pass2_result is not None
        assert result.text == "强制精炼文本"
        assert pass2_provider.call_count == 1

    def test_hotwords_support(self, tmp_path: Path) -> None:
        """测试热词支持"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        pass1_provider = MockASRProvider(text="包含专业术语的文本", confidence=0.90)

        engine = DictationEngine(pass1_provider)

        # 使用热词
        hotwords = ["专业术语", "技术名词"]
        result = engine.transcribe_utterance(wav_path, hotwords=hotwords)

        assert result.text == "包含专业术语的文本"

    def test_pass2_timeout(self, tmp_path: Path) -> None:
        """测试 pass2 超时处理"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        pass1_provider = MockASRProvider(text="原始文本", confidence=0.75)

        # 创建会超时的 pass2 provider
        class TimeoutProvider(ASRProvider):
            @property
            def is_cloud(self) -> bool:
                return True

            def transcribe_file(self, wav_path: Path, *, hotwords: list[str] | None = None) -> ASRResult:
                import time
                time.sleep(10)  # 模拟超时
                return ASRResult(text="不应该返回", confidence=0.98, model_name="timeout")

        pass2_provider = TimeoutProvider()

        # 配置短超时
        config = AppConfig()
        config.policy.pass2_timeout_ms_cloud = 100  # 100ms 超时

        engine = DictationEngine(pass1_provider, pass2_provider=pass2_provider, config=config)

        # 执行识别
        result = engine.transcribe_utterance(wav_path)

        # pass2 超时，应该使用 pass1 结果
        assert result.text == "原始文本"
        assert result.pass2_result is None

    def test_empty_text_handling(self, tmp_path: Path) -> None:
        """测试空文本处理"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        # pass1 返回空文本
        pass1_provider = MockASRProvider(text="", confidence=0.95)

        engine = DictationEngine(pass1_provider)

        result = engine.transcribe_utterance(wav_path)

        assert result.text == ""
        assert result.state == SessionState.COMMIT

    def test_pass2_empty_fallback_to_pass1(self, tmp_path: Path) -> None:
        """测试 pass2 返回空时回退到 pass1"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        pass1_provider = MockASRProvider(text="原始文本", confidence=0.75)
        pass2_provider = MockASRProvider(text="", confidence=0.98, is_cloud=True)

        engine = DictationEngine(pass1_provider, pass2_provider=pass2_provider)

        result = engine.transcribe_utterance(wav_path)

        # pass2 返回空，应该使用 pass1 结果
        assert result.text == "原始文本"

    def test_error_tracking_integration(self, tmp_path: Path) -> None:
        """测试错误追踪集成"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        # 创建会抛出异常的 provider
        class ErrorProvider(ASRProvider):
            @property
            def is_cloud(self) -> bool:
                return False

            def transcribe_file(self, wav_path: Path, *, hotwords: list[str] | None = None) -> ASRResult:
                raise RuntimeError("ASR 识别失败")

        pass1_provider = ErrorProvider()
        engine = DictationEngine(pass1_provider)

        # 应该抛出异常
        with pytest.raises(RuntimeError, match="ASR 识别失败"):
            engine.transcribe_utterance(wav_path)


class TestASRProviderChaining:
    """测试 ASR Provider 链式调用"""

    def test_multiple_pass2_attempts(self, tmp_path: Path) -> None:
        """测试多次 pass2 尝试"""
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"fake wav data")

        pass1_provider = MockASRProvider(text="原始文本", confidence=0.75)
        pass2_provider = MockASRProvider(text="精炼文本", confidence=0.98, is_cloud=True)

        engine = DictationEngine(pass1_provider, pass2_provider=pass2_provider)

        # 第一次识别
        result1 = engine.transcribe_utterance(wav_path)
        assert result1.text == "精炼文本"

        # 第二次识别（验证可以重复使用）
        result2 = engine.transcribe_utterance(wav_path)
        assert result2.text == "精炼文本"

        # 验证调用次数
        assert pass1_provider.call_count == 2
        assert pass2_provider.call_count == 2
