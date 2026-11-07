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
    assert result.raw_text == "测试文本"
    assert result.normalized_text == "测试文本"
    assert result.detected_language == "zh"
    assert "固定上下文" in fake_model.last_context
    assert "热词参考:" in fake_model.last_context
    assert "OpenClaw" in fake_model.last_context
    assert "小二" in fake_model.last_context


def test_qwen_asr_capabilities_declared() -> None:
    from recordian.providers.qwen_asr import QwenASRProvider

    capabilities = QwenASRProvider().capabilities

    assert capabilities.supports_hotwords is True
    assert capabilities.supports_context is True
    assert capabilities.supports_language_hint is True
    assert capabilities.supports_realtime is False


def _write_padded_wav(path: Path, *, speech_s: float = 1.0, pad_s: float = 1.0) -> None:
    """Write silence/speech/silence PCM16 WAV with the stdlib only."""
    import wave

    import numpy as np

    sr = 16000
    silence = np.zeros(int(pad_s * sr), dtype=np.float32)
    t = np.arange(int(speech_s * sr), dtype=np.float32) / sr
    speech = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    audio = np.concatenate([silence, speech, silence])
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _read_wav_mono(path: Path):  # noqa: ANN202
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0, sr


def test_apply_vad_energy_fallback_trims_silence(tmp_path: Path, monkeypatch) -> None:
    """Without a Silero model, the energy fallback still trims edges."""
    import pytest

    pytest.importorskip("torchaudio")
    from recordian.providers import qwen_asr
    from recordian.providers.qwen_asr import QwenASRProvider

    monkeypatch.setattr(qwen_asr, "_resolve_silero_vad_model", lambda: None)
    wav_path = tmp_path / "padded.wav"
    _write_padded_wav(wav_path)

    provider = QwenASRProvider()
    out = provider._apply_vad(wav_path)
    assert out != str(wav_path)

    samples, sr = _read_wav_mono(Path(out))
    # 3 s in, trimmed output keeps the speech core but drops most padding.
    assert samples.size < 2.2 * sr
    assert samples.size > 0.5 * sr


def test_silero_bounds_aggregate_segments_with_pad(monkeypatch, tmp_path: Path) -> None:
    """Bounds = first segment start / last segment end, padded, clamped.

    Uses a fake detector: Silero is a speech classifier and rejects
    synthetic tones, so segment plumbing is pinned here and the real
    model is exercised by the env-gated smoke test below.
    """
    import numpy as np
    import pytest

    sherpa_onnx = pytest.importorskip("sherpa_onnx")
    from recordian.providers import qwen_asr

    sr = 16000

    class _Seg:
        def __init__(self, start: int, n: int) -> None:
            self.start = start
            self.samples = np.zeros(n, dtype=np.float32)

    class _FakeDetector:
        def __init__(self, config, buffer_size_in_seconds) -> None:  # noqa: ANN001
            self._segs = [_Seg(sr, sr // 2), _Seg(2 * sr, sr // 4)]
            self.fed = 0

        def accept_waveform(self, samples) -> None:  # noqa: ANN001
            self.fed += len(samples)

        def flush(self) -> None:
            pass

        def empty(self) -> bool:
            return not self._segs

        @property
        def front(self):  # noqa: ANN202
            return self._segs[0]

        def pop(self) -> None:
            self._segs.pop(0)

    monkeypatch.setattr(sherpa_onnx, "VoiceActivityDetector", _FakeDetector)
    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"fake")
    samples = np.zeros(3 * sr, dtype=np.float32)
    bounds = qwen_asr._silero_speech_bounds(samples, sr, model)
    assert bounds is not None
    start, end = bounds
    pad = int(qwen_asr._SILERO_VAD_PAD_S * sr)
    assert start == sr - pad
    assert end == 2 * sr + sr // 4 + pad

    class _SilentDetector(_FakeDetector):
        def __init__(self, config, buffer_size_in_seconds) -> None:  # noqa: ANN001
            self._segs = []
            self.fed = 0

    monkeypatch.setattr(sherpa_onnx, "VoiceActivityDetector", _SilentDetector)
    assert qwen_asr._silero_speech_bounds(samples, sr, model) is None
    # Non-16k audio is out of scope for the Silero path.
    assert qwen_asr._silero_speech_bounds(samples, 8000, model) is None


def test_silero_bounds_real_model_smoke() -> None:
    """Real-model smoke test against a real speech WAV (env-gated).

    Synthetic tones/noise are rejected by the speech classifier, so
    this needs an actual utterance: set RECORDIAN_TEST_SPEECH_WAV.
    """
    import os

    import pytest

    pytest.importorskip("sherpa_onnx")
    from recordian.providers.qwen_asr import _resolve_silero_vad_model, _silero_speech_bounds

    speech_wav = os.environ.get("RECORDIAN_TEST_SPEECH_WAV", "")
    if not speech_wav:
        pytest.skip("RECORDIAN_TEST_SPEECH_WAV not set")
    model = _resolve_silero_vad_model()
    if model is None:
        pytest.skip("silero_vad.onnx not provisioned in models/")
    samples, sr = _read_wav_mono(Path(speech_wav))
    bounds = _silero_speech_bounds(samples, sr, model)
    assert bounds is not None
    start, end = bounds
    assert 0 <= start < end <= samples.size
