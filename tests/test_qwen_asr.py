import inspect
from pathlib import Path


def test_qwen_asr_no_debug_print() -> None:
    """qwen_asr.py 不应包含 [DEBUG] print 语句"""
    from recordian.providers import qwen_asr
    source = inspect.getsource(qwen_asr)
    assert "[DEBUG]" not in source, "qwen_asr.py 仍包含 [DEBUG] print 语句"


def test_compose_qwen_context_includes_hotwords_once() -> None:
    from recordian.providers.qwen_asr import _compose_qwen_context

    context = _compose_qwen_context("会议纪要", [" 小二 ", "OpenClaw", "小二"])
    assert context.startswith("会议纪要\n热词参考:")
    assert context.count("小二") == 1
    assert "OpenClaw" in context


def test_qwen_asr_transcribe_injects_hotwords_to_context(tmp_path: Path, monkeypatch) -> None:
    from recordian.providers.qwen_asr import QwenASRProvider

    class _Result:
        text = "测试文本"
        language = "zh"

    class _FakeModel:
        def __init__(self) -> None:
            self.last_context = ""

        def transcribe(self, *, audio, context, language, return_time_stamps):  # noqa: ANN001
            self.last_context = context
            return [_Result()]

    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFF")

    provider = QwenASRProvider(context="固定上下文")
    fake_model = _FakeModel()
    provider._model = fake_model
    monkeypatch.setattr(provider, "_lazy_load", lambda: None)

    result = provider.transcribe_file(wav_path, hotwords=["OpenClaw", "小二", "OpenClaw"])
    assert result.text == "测试文本"
    assert "固定上下文" in fake_model.last_context
    assert "热词参考:" in fake_model.last_context
    assert "OpenClaw" in fake_model.last_context
    assert "小二" in fake_model.last_context
