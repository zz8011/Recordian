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
