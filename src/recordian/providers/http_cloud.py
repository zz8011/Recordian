from __future__ import annotations

import base64
import json
from pathlib import Path

from .base import ASRProvider, _estimate_english_ratio
from ..models import ASRResult


class HttpCloudProvider(ASRProvider):
    """Generic HTTP provider.

    Request JSON:
    {
      "audio_base64": "...",
      "hotwords": [...]
    }

    Response JSON example:
    {
      "text": "...",
      "confidence": 0.92,
      "model": "cloud-asr-v1"
    }
    """

    def __init__(self, endpoint: str, *, api_key: str | None = None, timeout_s: float = 10.0) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_s = timeout_s

    @property
    def provider_name(self) -> str:
        return "http-cloud"

    @property
    def is_cloud(self) -> bool:
        return True

    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult:
        if not wav_path.exists():
            raise FileNotFoundError(wav_path)

        try:
            import requests
        except ImportError:
            raise ImportError("requests library is required for HttpCloudProvider. Install with: pip install requests")

        audio_data = wav_path.read_bytes()
        payload = {
            "audio_base64": base64.b64encode(audio_data).decode("ascii"),
            "hotwords": hotwords,
        }

        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = requests.post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=self.timeout_s
        )
        response.raise_for_status()
        body = response.json()

        text = str(body.get("text", "")).strip()
        confidence = body.get("confidence")
        model_name = str(body.get("model", "cloud"))

        return ASRResult(
            text=text,
            confidence=confidence if isinstance(confidence, (int, float)) else None,
            english_ratio=_estimate_english_ratio(text),
            model_name=model_name,
            metadata={"raw": body, "source": "cloud_http"},
        )

