from __future__ import annotations

from pathlib import Path

from .base import ASRProvider, _estimate_english_ratio
from ..models import ASRResult


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
        max_new_tokens: int = 1024,
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
            fd, temp_path = tempfile.mkstemp(suffix='.wav')
            import os
            os.close(fd)

            torchaudio.save(temp_path, trimmed, sample_rate)
            return temp_path

        except Exception:
            # VAD 失败，返回原音频
            return str(wav_path)

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        self._lazy_load()

        if not wav_path.exists():
            raise FileNotFoundError(wav_path)

        # 使用 VAD 预处理音频，移除静音部分
        processed_audio = self._apply_vad(wav_path)

        results = self._model.transcribe(
            audio=processed_audio,
            context=self.context,
            language=self.language,
            return_time_stamps=False,
        )

        result = results[0]
        text = (result.text or "").strip()
        metadata: dict[str, object] = {"source": "qwen_asr"}
        if hasattr(result, "language"):
            metadata["detected_language"] = result.language

        return ASRResult(
            text=text,
            confidence=None,
            english_ratio=_estimate_english_ratio(text),
            model_name=self.model_name,
            metadata=metadata,
        )

