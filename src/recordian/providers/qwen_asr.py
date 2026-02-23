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

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        self._lazy_load()

        if not wav_path.exists():
            raise FileNotFoundError(wav_path)


        results = self._model.transcribe(
            audio=str(wav_path),
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

