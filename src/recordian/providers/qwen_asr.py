from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from ..models import ASRResult
from .asr_context import ASRContextComposer
from .base import ASRProvider, ASRProviderCapabilities, _estimate_english_ratio

_STREAM_SAMPLE_RATE = 16000
_STREAM_MIN_SAMPLES = int(_STREAM_SAMPLE_RATE * 0.45)
_STREAM_PARTIAL_NEW_SAMPLES = int(_STREAM_SAMPLE_RATE * 0.7)
_STREAM_PARTIAL_MAX_SAMPLES = int(_STREAM_SAMPLE_RATE * 24)


def _compose_qwen_context(base_context: str, hotwords: list[str], *, max_hotwords: int = 40) -> str:
    return ASRContextComposer(base_context, max_hotwords=max_hotwords).compose_text(hotwords)


class QwenASRProvider(ASRProvider):
    """Qwen3-ASR local transcription provider (transformers backend).

    Uses the ``qwen-asr`` package.  Install with::

        pip install -e '.[qwen-asr]'
    """

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-ASR-0.6B",
        *,
        device: str = "cuda:0",
        dtype: str = "bfloat16",
        language: str | None = None,
        max_new_tokens: int = 8192,
        max_inference_batch_size: int = 1,
        context: str = "",
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.dtype = dtype
        self.language = language
        self.max_new_tokens = max_new_tokens
        self.max_inference_batch_size = max_inference_batch_size
        self.context = context
        self._model = None

    @property
    def provider_name(self) -> str:
        return f"qwen-asr:{self.model_name}"

    @property
    def capabilities(self) -> ASRProviderCapabilities:
        return ASRProviderCapabilities(
            supports_hotwords=True,
            supports_context=True,
            supports_language_hint=True,
            supports_realtime=True,
        )

    def start_realtime_session(self, *, hotwords: list[str]) -> "_QwenRealtimeSession":
        self._lazy_load()
        return _QwenRealtimeSession(self, hotwords)

    def _lazy_load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from qwen_asr import Qwen3ASRModel
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "qwen-asr 未安装。请执行: pip install -e '.[qwen-asr]'"
            ) from exc

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        torch_dtype = dtype_map.get(self.dtype, torch.bfloat16)

        self._model = Qwen3ASRModel.from_pretrained(
            self.model_name,
            dtype=torch_dtype,
            device_map=self.device,
            max_new_tokens=self.max_new_tokens,
            max_inference_batch_size=self.max_inference_batch_size,
        )

    def _apply_vad(self, wav_path: Path) -> str:
        """使用 VAD 移除静音部分，返回处理后的音频路径或原路径"""
        try:
            import torch
            import torchaudio

            # 加载音频
            waveform, sample_rate = torchaudio.load(str(wav_path))

            # 如果音频太短（<0.5秒），直接返回
            if waveform.shape[1] < sample_rate * 0.5:
                return str(wav_path)

            # 简单的能量阈值 VAD
            # 计算每帧的能量
            frame_length = int(sample_rate * 0.02)  # 20ms
            hop_length = int(sample_rate * 0.01)    # 10ms

            # 计算 RMS 能量
            energy = []
            for i in range(0, waveform.shape[1] - frame_length, hop_length):
                frame = waveform[:, i:i+frame_length]
                rms = torch.sqrt(torch.mean(frame ** 2))
                energy.append(rms.item())

            if not energy:
                return str(wav_path)

            # 动态阈值：平均能量的 20%
            threshold = sum(energy) / len(energy) * 0.2

            # 找到有声音的区域
            speech_frames = [i for i, e in enumerate(energy) if e > threshold]

            if not speech_frames:
                return str(wav_path)

            # 扩展边界（前后各 5 帧）
            start_frame = max(0, speech_frames[0] - 5)
            end_frame = min(len(energy), speech_frames[-1] + 5)

            # 转换为样本索引
            start_sample = start_frame * hop_length
            end_sample = min(waveform.shape[1], end_frame * hop_length + frame_length)

            # 裁剪音频
            trimmed = waveform[:, start_sample:end_sample]

            # 如果裁剪后太短，返回原音频
            if trimmed.shape[1] < sample_rate * 0.3:
                return str(wav_path)

            # 保存到临时文件
            import tempfile
            temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            temp_path = temp_file.name
            temp_file.close()

            torchaudio.save(temp_path, trimmed, sample_rate)
            return temp_path

        except Exception:
            # VAD 失败，返回原音频
            return str(wav_path)

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        self._lazy_load()
        assert self._model is not None, "model should be loaded after _lazy_load"

        if not wav_path.exists():
            raise FileNotFoundError(wav_path)

        # 使用 VAD 预处理音频，移除静音部分
        processed_audio = self._apply_vad(wav_path)
        vad_temp_file = None if processed_audio == str(wav_path) else processed_audio

        try:
            context = ASRContextComposer(self.context).compose_text(hotwords)
            results = self._model.transcribe(
                audio=processed_audio,
                context=context,
                language=self.language,
                return_time_stamps=False,
            )

            result = results[0]
            text = (result.text or "").strip()
            detected_language = getattr(result, "language", None)
            metadata: dict[str, object] = {"source": "qwen_asr"}
            if detected_language:
                metadata["detected_language"] = detected_language

            return ASRResult(
                text=text,
                confidence=None,
                english_ratio=_estimate_english_ratio(text),
                model_name=self.model_name,
                detected_language=str(detected_language).strip() or None if detected_language is not None else None,
                metadata=metadata,
            )
        finally:
            # 清理 VAD 临时文件
            if vad_temp_file is not None:
                import os
                try:
                    os.unlink(vad_temp_file)
                except OSError:
                    pass

    def _transcribe_array(self, audio: np.ndarray, hotwords: list[str], *, max_samples: int | None = None) -> str:
        self._lazy_load()
        assert self._model is not None
        if max_samples is not None and audio.size > max_samples:
            audio = audio[-max_samples:]
        if audio.size < _STREAM_MIN_SAMPLES:
            return ""
        context = ASRContextComposer(self.context).compose_text(hotwords)
        results = self._model.transcribe(
            audio=(audio, _STREAM_SAMPLE_RATE),
            context=context,
            language=self.language,
            return_time_stamps=False,
        )
        result = results[0]
        return (result.text or "").strip()


class _QwenRealtimeSession:
    """Feed float32 PCM and return the current hypothesis.

    The transformers backend has no token stream. Each update transcribes the
    audio gathered so far, which is what the input box shows while the key is held.
    """

    def __init__(self, provider: QwenASRProvider, hotwords: list[str]) -> None:
        self._provider = provider
        self._hotwords = list(hotwords)
        self._pcm = np.zeros((0,), dtype=np.float32)
        self._last_text = ""
        self._last_infer_samples = 0
        self._started_at = time.perf_counter()

    def push_audio(self, raw: bytes) -> dict[str, str]:
        if raw:
            chunk = np.frombuffer(raw, dtype="<f4")
            if chunk.size:
                self._pcm = np.concatenate([self._pcm, np.array(chunk, dtype=np.float32, copy=True)])
        new_samples = self._pcm.size - self._last_infer_samples
        if self._last_text and new_samples < _STREAM_PARTIAL_NEW_SAMPLES:
            return {"text": self._last_text}
        if self._pcm.size < _STREAM_MIN_SAMPLES:
            return {"text": ""}
        audio = self._pcm
        if audio.size > _STREAM_PARTIAL_MAX_SAMPLES:
            audio = audio[-_STREAM_PARTIAL_MAX_SAMPLES:]
        text = self._provider._transcribe_array(
            audio,
            self._hotwords,
            max_samples=_STREAM_PARTIAL_MAX_SAMPLES,
        )
        self._last_infer_samples = self._pcm.size
        if text:
            self._last_text = text
        return {"text": self._last_text}

    def finish(self) -> SimpleNamespace:
        text = self._last_text
        if self._pcm.size >= _STREAM_MIN_SAMPLES:
            text = self._provider._transcribe_array(self._pcm, self._hotwords) or text
        return SimpleNamespace(text=text, detected_language=self._provider.language)

    def cancel(self) -> None:
        self._pcm = np.zeros((0,), dtype=np.float32)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._started_at) * 1000.0
