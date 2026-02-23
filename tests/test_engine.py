from pathlib import Path

from recordian.engine import DictationEngine
from recordian.models import ASRResult
from recordian.providers.base import ASRProvider


class FakeProvider(ASRProvider):
    def __init__(self, name: str, text: str, confidence: float | None = None) -> None:
        self._name = name
        self._text = text
        self._confidence = confidence

    @property
    def provider_name(self) -> str:
        return self._name

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        return ASRResult(text=self._text, confidence=self._confidence, model_name=self._name)


def test_engine_uses_pass2_when_triggered() -> None:
    pass1 = FakeProvider("p1", text="123", confidence=0.4)
    pass2 = FakeProvider("p2", text="一二三", confidence=0.9)
    engine = DictationEngine(pass1, pass2_provider=pass2)

    result = engine.transcribe_utterance(Path(__file__), hotwords=[])

    assert result.decision.run_pass2 is True
    assert result.text == "一二三"
