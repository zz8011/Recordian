from pathlib import Path

from recordian.models import ASRResult
from recordian.providers.base import ASRProvider
from recordian.providers.streaming_base import StreamingASRProvider
from recordian.realtime import RealtimeDictationEngine


class FakeStreamingProvider(StreamingASRProvider):
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.idx = 0
        self._last = ""

    @property
    def provider_name(self) -> str:
        return "fake-stream"

    def start_session(self, *, hotwords: list[str]) -> None:
        self.idx = 0
        self._last = ""

    def push_chunk(self, samples: list[float], *, is_final: bool, chunk_index: int):
        text = self.outputs[self.idx] if self.idx < len(self.outputs) else ""
        self.idx += 1
        if text:
            self._last = text
        from recordian.models import StreamUpdate

        return StreamUpdate(text=text, is_final=is_final, chunk_index=chunk_index)

    def end_session(self) -> ASRResult:
        return ASRResult(text=self._last, confidence=0.95, model_name="fake-stream")


class FakePass2Provider(ASRProvider):
    @property
    def provider_name(self) -> str:
        return "fake-pass2"

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        return ASRResult(text="最终修正文本", confidence=0.9, model_name="fake-pass2")


def test_realtime_engine_runs_pass2_when_forced() -> None:
    engine = RealtimeDictationEngine(
        FakeStreamingProvider(["你", "你好", "你好世界"]),
        pass2_provider=FakePass2Provider(),
    )
    chunks = [[0.1] * 800, [0.1] * 800, [0.1] * 800]
    result = engine.transcribe_chunks(chunks, force_high_precision=True)

    assert result.commit.decision.run_pass2 is True
    assert result.commit.text == "最终修正文本"
    assert len(result.updates) == 3
