from pathlib import Path
import time
from unittest.mock import Mock, patch

import pytest

from recordian.engine import DictationEngine
from recordian.models import ASRResult
from recordian.providers.base import ASRProvider


class FakeProvider(ASRProvider):
    def __init__(self, name: str, text: str, confidence: float | None = None, delay: float = 0.0, is_cloud: bool = False) -> None:
        self._name = name
        self._text = text
        self._confidence = confidence
        self._delay = delay
        self._is_cloud = is_cloud

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def is_cloud(self) -> bool:
        return self._is_cloud

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        if self._delay > 0:
            time.sleep(self._delay)
        return ASRResult(text=self._text, confidence=self._confidence, model_name=self._name)


def test_engine_uses_pass2_when_triggered() -> None:
    pass1 = FakeProvider("p1", text="123", confidence=0.4)
    pass2 = FakeProvider("p2", text="一二三", confidence=0.9)
    engine = DictationEngine(pass1, pass2_provider=pass2)

    result = engine.transcribe_utterance(Path(__file__), hotwords=[])

    assert result.decision.run_pass2 is True
    assert result.text == "一二三"


class TestEngineTimeoutHandling:
    """测试 Pass2 超时处理"""

    def test_pass2_timeout_fallback_to_pass1(self) -> None:
        """测试 Pass2 超时时降级到 Pass1 结果"""
        pass1 = FakeProvider("p1", text="快速结果", confidence=0.6)
        # Pass2 延迟 2 秒，超过默认超时
        pass2 = FakeProvider("p2", text="慢速结果", confidence=0.9, delay=2.0)

        from recordian.config import AppConfig
        config = AppConfig()
        config.policy.pass2_timeout_ms_local = 500  # 500ms 超时

        engine = DictationEngine(pass1, pass2_provider=pass2, config=config)

        start = time.time()
        result = engine.transcribe_utterance(Path(__file__), hotwords=[], force_high_precision=True)
        elapsed = time.time() - start

        # 验证超时后使用 Pass1 结果
        assert result.text == "快速结果"
        assert result.pass2_result is None
        # 验证在超时时间内返回（加上一些容差）
        assert elapsed < 1.5

    def test_pass2_cloud_timeout_uses_cloud_config(self) -> None:
        """测试云端 Pass2 使用云端超时配置"""
        pass1 = FakeProvider("p1", text="本地结果", confidence=0.6)
        pass2 = FakeProvider("p2", text="云端结果", confidence=0.9, delay=1.5, is_cloud=True)

        from recordian.config import AppConfig
        config = AppConfig()
        config.policy.pass2_timeout_ms_cloud = 2000  # 云端 2 秒超时
        config.policy.pass2_timeout_ms_local = 500   # 本地 500ms 超时

        engine = DictationEngine(pass1, pass2_provider=pass2, config=config)

        result = engine.transcribe_utterance(Path(__file__), hotwords=[], force_high_precision=True)

        # 验证使用云端超时，Pass2 成功完成
        assert result.text == "云端结果"
        assert result.pass2_result is not None

    def test_negative_timeout_value_handled(self) -> None:
        """测试负数超时值的处理"""
        pass1 = FakeProvider("p1", text="结果", confidence=0.6)
        pass2 = FakeProvider("p2", text="Pass2结果", confidence=0.9, delay=0.1)

        from recordian.config import AppConfig
        config = AppConfig()
        config.policy.pass2_timeout_ms_local = -1000  # 负数超时

        engine = DictationEngine(pass1, pass2_provider=pass2, config=config)

        # 负数超时应该立即超时，使用 Pass1 结果
        result = engine.transcribe_utterance(Path(__file__), hotwords=[], force_high_precision=True)

        # 验证立即超时
        assert result.text == "结果"
        assert result.pass2_result is None

    def test_extremely_large_timeout_value(self) -> None:
        """测试超大超时值不会导致问题"""
        pass1 = FakeProvider("p1", text="结果", confidence=0.6)
        pass2 = FakeProvider("p2", text="Pass2结果", confidence=0.9, delay=0.1)

        from recordian.config import AppConfig
        config = AppConfig()
        config.policy.pass2_timeout_ms_local = 999999999  # 超大超时值

        engine = DictationEngine(pass1, pass2_provider=pass2, config=config)

        # 应该正常完成，不会真的等待那么久
        start = time.time()
        result = engine.transcribe_utterance(Path(__file__), hotwords=[], force_high_precision=True)
        elapsed = time.time() - start

        # 验证快速完成
        assert result.text == "Pass2结果"
        assert elapsed < 1.0

    def test_zero_timeout_immediate_fallback(self) -> None:
        """测试零超时立即降级"""
        pass1 = FakeProvider("p1", text="Pass1结果", confidence=0.6)
        pass2 = FakeProvider("p2", text="Pass2结果", confidence=0.9, delay=0.5)

        from recordian.config import AppConfig
        config = AppConfig()
        config.policy.pass2_timeout_ms_local = 0  # 零超时

        engine = DictationEngine(pass1, pass2_provider=pass2, config=config)

        result = engine.transcribe_utterance(Path(__file__), hotwords=[], force_high_precision=True)

        # 验证立即超时，使用 Pass1
        assert result.text == "Pass1结果"
        assert result.pass2_result is None

    def test_pass2_completes_within_timeout(self) -> None:
        """测试 Pass2 在超时内完成"""
        pass1 = FakeProvider("p1", text="Pass1结果", confidence=0.6)
        pass2 = FakeProvider("p2", text="Pass2结果", confidence=0.9, delay=0.2)

        from recordian.config import AppConfig
        config = AppConfig()
        config.policy.pass2_timeout_ms_local = 1000  # 1 秒超时

        engine = DictationEngine(pass1, pass2_provider=pass2, config=config)

        result = engine.transcribe_utterance(Path(__file__), hotwords=[], force_high_precision=True)

        # 验证 Pass2 成功完成
        assert result.text == "Pass2结果"
        assert result.pass2_result is not None
        assert result.pass2_result.text == "Pass2结果"
