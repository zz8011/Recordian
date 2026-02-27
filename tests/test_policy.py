from recordian.config import Pass2PolicyConfig
from recordian.models import ASRResult, SessionContext
from recordian.policy import Pass2Policy


def test_force_high_precision_triggers_pass2() -> None:
    policy = Pass2Policy(Pass2PolicyConfig())
    result = ASRResult(text="你好", confidence=0.99)
    ctx = SessionContext(force_high_precision=True)

    decision = policy.evaluate(result, ctx)

    assert decision.run_pass2 is True
    assert "forced_high_precision" in decision.reasons


def test_low_confidence_triggers_pass2() -> None:
    policy = Pass2Policy(Pass2PolicyConfig(confidence_threshold=0.9))
    result = ASRResult(text="你好", confidence=0.5)
    ctx = SessionContext()

    decision = policy.evaluate(result, ctx)

    assert decision.run_pass2 is True
    assert "low_confidence" in decision.reasons


def test_clean_high_confidence_can_skip_pass2() -> None:
    policy = Pass2Policy(Pass2PolicyConfig())
    result = ASRResult(text="今天下午开会", confidence=0.98, english_ratio=0.0)
    ctx = SessionContext(hotwords=["开会"])

    decision = policy.evaluate(result, ctx)

    assert decision.run_pass2 is False
    assert decision.reasons == []


def test_confidence_threshold_boundary() -> None:
    """测试 confidence=0.88 临界点"""
    policy = Pass2Policy(Pass2PolicyConfig(confidence_threshold=0.88))

    # 刚好等于阈值，应该不触发 pass2
    result_equal = ASRResult(text="测试", confidence=0.88)
    ctx = SessionContext()
    decision = policy.evaluate(result_equal, ctx)
    assert decision.run_pass2 is False

    # 略低于阈值，应该触发 pass2
    result_below = ASRResult(text="测试", confidence=0.87)
    decision = policy.evaluate(result_below, ctx)
    assert decision.run_pass2 is True
    assert "low_confidence" in decision.reasons

    # 略高于阈值，应该不触发 pass2
    result_above = ASRResult(text="测试", confidence=0.89)
    decision = policy.evaluate(result_above, ctx)
    assert decision.run_pass2 is False


def test_multiple_conditions_combination() -> None:
    """测试多条件组合"""
    policy = Pass2Policy(Pass2PolicyConfig(
        confidence_threshold=0.9,
        english_ratio_threshold=0.3,
    ))

    # 低置信度 + 高英文比例
    result = ASRResult(text="hello world", confidence=0.85, english_ratio=0.8)
    ctx = SessionContext()
    decision = policy.evaluate(result, ctx)
    assert decision.run_pass2 is True
    assert "low_confidence" in decision.reasons
    assert "high_english_ratio" in decision.reasons

    # 高置信度 + 高英文比例
    result = ASRResult(text="hello world", confidence=0.95, english_ratio=0.8)
    decision = policy.evaluate(result, ctx)
    assert decision.run_pass2 is True
    assert "high_english_ratio" in decision.reasons
    assert "low_confidence" not in decision.reasons


def test_hotwords_matching() -> None:
    """测试热词匹配"""
    policy = Pass2Policy(Pass2PolicyConfig())

    # 包含热词
    result = ASRResult(text="今天开会讨论项目", confidence=0.95)
    ctx = SessionContext(hotwords=["开会", "项目"])
    decision = policy.evaluate(result, ctx)
    # 高置信度 + 包含热词 = 不需要 pass2
    assert decision.run_pass2 is False

    # 不包含热词
    result = ASRResult(text="今天吃饭", confidence=0.95)
    ctx = SessionContext(hotwords=["开会", "项目"])
    decision = policy.evaluate(result, ctx)
    # 高置信度但不包含热词，会触发 pass2（因为热词缺失）
    assert decision.run_pass2 is True
    assert "hotword_missing" in decision.reasons


def test_empty_text_handling() -> None:
    """测试空文本处理"""
    policy = Pass2Policy(Pass2PolicyConfig())

    result = ASRResult(text="", confidence=0.95)
    ctx = SessionContext()
    decision = policy.evaluate(result, ctx)
    # 空文本不应该触发 pass2
    assert decision.run_pass2 is False


def test_english_ratio_boundary() -> None:
    """测试英文比例边界"""
    policy = Pass2Policy(Pass2PolicyConfig(english_ratio_threshold=0.5))

    # 刚好等于阈值
    result = ASRResult(text="test", confidence=0.95, english_ratio=0.5)
    ctx = SessionContext()
    decision = policy.evaluate(result, ctx)
    assert decision.run_pass2 is False

    # 略高于阈值
    result = ASRResult(text="test", confidence=0.95, english_ratio=0.51)
    decision = policy.evaluate(result, ctx)
    assert decision.run_pass2 is True
    assert "high_english_ratio" in decision.reasons
