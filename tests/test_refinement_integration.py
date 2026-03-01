"""文本精炼集成测试"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from recordian.models import ASRResult, SessionContext
from recordian.policy import Pass2Policy, PolicyConfig


class TestTextRefinementIntegration:
    """测试文本精炼集成"""

    def test_low_confidence_triggers_refinement(self) -> None:
        """测试低置信度触发精炼"""
        config = PolicyConfig()
        config.confidence_threshold = 0.88
        policy = Pass2Policy(config)

        # 低置信度结果
        asr_result = ASRResult(text="测试文本", confidence=0.75, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        assert decision.run_pass2 is True
        assert "low_confidence" in decision.reasons

    def test_high_confidence_skips_refinement(self) -> None:
        """测试高置信度跳过精炼"""
        config = PolicyConfig()
        config.confidence_threshold = 0.88
        policy = Pass2Policy(config)

        # 高置信度结果
        asr_result = ASRResult(text="测试文本", confidence=0.95, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        assert decision.run_pass2 is False
        assert len(decision.reasons) == 0

    def test_high_english_ratio_triggers_refinement(self) -> None:
        """测试高英文比例触发精炼"""
        config = PolicyConfig()
        config.english_ratio_threshold = 0.15
        policy = Pass2Policy(config)

        # 高英文比例文本
        asr_result = ASRResult(
            text="This is a test with English content",
            confidence=0.95,
            model_name="test"
        )
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        assert decision.run_pass2 is True
        assert "high_english_ratio" in decision.reasons

    def test_force_high_precision_triggers_refinement(self) -> None:
        """测试强制高精度触发精炼"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 即使高置信度
        asr_result = ASRResult(text="测试文本", confidence=0.98, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=True)

        decision = policy.evaluate(asr_result, context)

        assert decision.run_pass2 is True
        assert "force_high_precision" in decision.reasons

    def test_short_text_skips_refinement(self) -> None:
        """测试短文本跳过精炼"""
        config = PolicyConfig()
        config.min_text_length = 5
        policy = Pass2Policy(config)

        # 短文本，即使低置信度
        asr_result = ASRResult(text="测试", confidence=0.75, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 短文本应该跳过精炼
        assert decision.run_pass2 is False

    def test_empty_text_skips_refinement(self) -> None:
        """测试空文本跳过精炼"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 空文本
        asr_result = ASRResult(text="", confidence=0.75, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        assert decision.run_pass2 is False

    def test_multiple_reasons_for_refinement(self) -> None:
        """测试多个触发精炼的原因"""
        config = PolicyConfig()
        config.confidence_threshold = 0.88
        config.english_ratio_threshold = 0.15
        policy = Pass2Policy(config)

        # 低置信度 + 高英文比例
        asr_result = ASRResult(
            text="This is low confidence English text",
            confidence=0.75,
            model_name="test"
        )
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        assert decision.run_pass2 is True
        assert "low_confidence" in decision.reasons
        assert "high_english_ratio" in decision.reasons
        assert len(decision.reasons) >= 2

    def test_confidence_threshold_boundary(self) -> None:
        """测试置信度阈值边界"""
        config = PolicyConfig()
        config.confidence_threshold = 0.88
        policy = Pass2Policy(config)

        # 刚好等于阈值
        asr_result1 = ASRResult(text="测试文本", confidence=0.88, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)
        decision1 = policy.evaluate(asr_result1, context)
        assert decision1.run_pass2 is False

        # 略低于阈值
        asr_result2 = ASRResult(text="测试文本", confidence=0.87, model_name="test")
        decision2 = policy.evaluate(asr_result2, context)
        assert decision2.run_pass2 is True

    def test_english_ratio_calculation(self) -> None:
        """测试英文比例计算"""
        config = PolicyConfig()
        config.english_ratio_threshold = 0.5
        policy = Pass2Policy(config)

        # 50% 英文
        asr_result = ASRResult(text="Hello世界", confidence=0.95, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 应该触发精炼
        assert decision.run_pass2 is True

    def test_policy_config_update(self) -> None:
        """测试策略配置更新"""
        config = PolicyConfig()
        config.confidence_threshold = 0.88
        policy = Pass2Policy(config)

        asr_result = ASRResult(text="测试文本", confidence=0.85, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        # 第一次评估
        decision1 = policy.evaluate(asr_result, context)
        assert decision1.run_pass2 is True

        # 更新配置
        config.confidence_threshold = 0.80
        policy = Pass2Policy(config)

        # 第二次评估
        decision2 = policy.evaluate(asr_result, context)
        assert decision2.run_pass2 is False  # 现在置信度足够高


class TestRefinementEdgeCases:
    """测试精炼边界情况"""

    def test_unicode_text_handling(self) -> None:
        """测试 Unicode 文本处理"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 包含各种 Unicode 字符
        asr_result = ASRResult(
            text="测试文本 🎉 emoji 和特殊字符 ©®™",
            confidence=0.95,
            model_name="test"
        )
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 应该能正常处理
        assert decision is not None

    def test_very_long_text(self) -> None:
        """测试超长文本"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 生成超长文本
        long_text = "测试文本" * 1000
        asr_result = ASRResult(text=long_text, confidence=0.95, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 应该能正常处理
        assert decision is not None

    def test_mixed_language_text(self) -> None:
        """测试混合语言文本"""
        config = PolicyConfig()
        config.english_ratio_threshold = 0.3
        policy = Pass2Policy(config)

        # 中英混合
        asr_result = ASRResult(
            text="这是一段中英混合的text，包含English和中文",
            confidence=0.95,
            model_name="test"
        )
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 根据英文比例决定是否精炼
        assert decision is not None

    def test_special_characters_only(self) -> None:
        """测试仅包含特殊字符"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 仅特殊字符
        asr_result = ASRResult(text="!@#$%^&*()", confidence=0.95, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 应该能正常处理
        assert decision is not None

    def test_whitespace_only_text(self) -> None:
        """测试仅包含空白字符"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 仅空白字符
        asr_result = ASRResult(text="   \t\n  ", confidence=0.95, model_name="test")
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 应该跳过精炼
        assert decision.run_pass2 is False

    def test_numbers_and_punctuation(self) -> None:
        """测试数字和标点符号"""
        config = PolicyConfig()
        policy = Pass2Policy(config)

        # 数字和标点
        asr_result = ASRResult(
            text="价格是 123.45 元，折扣 20%。",
            confidence=0.95,
            model_name="test"
        )
        context = SessionContext(hotwords=[], force_high_precision=False)

        decision = policy.evaluate(asr_result, context)

        # 应该能正常处理
        assert decision is not None
