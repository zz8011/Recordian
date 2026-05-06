from recordian.providers.asr_context import ASRContextComposer


def test_asr_context_composer_dedupes_and_limits_hotwords() -> None:
    composer = ASRContextComposer("会议纪要", max_hotwords=2)

    text = composer.compose_text([" 小二 ", "OpenClaw", "小二", "Recordian"])

    assert text == "会议纪要\n热词参考: 小二、OpenClaw"


def test_asr_context_composer_returns_base_context_when_hotwords_empty() -> None:
    composer = ASRContextComposer("固定上下文")

    assert composer.compose_text([]) == "固定上下文"
