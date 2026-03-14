from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from shutil import which
from urllib.parse import urlparse, urlunparse

from ..models import ASRResult
from .base import ASRProvider, _estimate_english_ratio


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

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        model_name: str = "",
        language: str = "",
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.model_name = model_name.strip() or "Qwen/Qwen3-ASR-1.7B"
        self.language = language.strip()
        self._resolved_openai_model_name: str | None = None

    def _is_openai_transcription_endpoint(self) -> bool:
        path = urlparse(self.endpoint).path.lower()
        return path.endswith("/v1/audio/transcriptions") or path.endswith("/audio/transcriptions")

    def _openai_models_endpoint(self) -> str | None:
        if not self._is_openai_transcription_endpoint():
            return None

        parsed = urlparse(self.endpoint)
        suffix = "/audio/transcriptions"
        if not parsed.path.lower().endswith(suffix):
            return None
        models_path = parsed.path[:-len(suffix)] + "/models"
        return urlunparse(parsed._replace(path=models_path, params="", query="", fragment=""))

    def _candidate_model_names(self) -> list[str]:
        candidates: list[str] = []
        primary = self.model_name.strip()
        if primary:
            candidates.append(primary)
            if "/" in primary:
                short_name = primary.rsplit("/", 1)[-1].strip()
                if short_name and short_name not in candidates:
                    candidates.append(short_name)
        return candidates or ["cloud-asr"]

    def _resolve_openai_model_name(self, requests_module, headers: dict[str, str]) -> str:  # noqa: ANN001
        if self._resolved_openai_model_name:
            return self._resolved_openai_model_name

        candidates = self._candidate_model_names()
        models_endpoint = self._openai_models_endpoint()
        if not models_endpoint:
            return candidates[0]

        try:
            response = requests_module.get(
                models_endpoint,
                headers=headers,
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except Exception:
            return candidates[0]

        available_ids: list[str] = []
        data = body.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("id")
                if isinstance(model_id, str):
                    normalized = model_id.strip()
                    if normalized:
                        available_ids.append(normalized)

        for candidate in candidates:
            if candidate in available_ids:
                self._resolved_openai_model_name = candidate
                return candidate

        if len(available_ids) == 1:
            self._resolved_openai_model_name = available_ids[0]
            return available_ids[0]

        return candidates[0]

    def _prepare_openai_audio_file(self, audio_path: Path) -> tuple[bytes, str, str]:
        """Prepare audio payload for OpenAI-compatible transcription endpoint.

        vLLM may reject some OGG/Opus recordings as malformed. To improve
        compatibility, we transcode non-WAV input to 16k mono WAV when ffmpeg
        is available.
        """
        suffix = audio_path.suffix.lower()
        raw = audio_path.read_bytes()
        if suffix == ".wav":
            return raw, audio_path.name, "audio/wav"

        ffmpeg_bin = which("ffmpeg")
        if ffmpeg_bin:
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "pipe:1",
            ]
            proc = subprocess.run(cmd, capture_output=True, check=False)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout, f"{audio_path.stem}.wav", "audio/wav"

        mime_map = {
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
            ".mp3": "audio/mpeg",
            ".flac": "audio/flac",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".wav": "audio/wav",
        }
        return raw, audio_path.name, mime_map.get(suffix, "application/octet-stream")

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
        except ImportError as exc:
            raise ImportError(
                "requests library is required for HttpCloudProvider. Install with: pip install requests"
            ) from exc

        audio_data = wav_path.read_bytes()

        headers = {
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self._is_openai_transcription_endpoint():
            # OpenAI-compatible transcription API (e.g. vLLM /v1/audio/transcriptions)
            upload_data, upload_name, upload_mime = self._prepare_openai_audio_file(wav_path)
            form_data: dict[str, str] = {
                "model": self._resolve_openai_model_name(requests, headers),
            }
            if self.language and self.language.lower() != "auto":
                lang_map = {"chinese": "zh", "english": "en"}
                normalized = lang_map.get(self.language.lower(), self.language)
                form_data["language"] = normalized
            if hotwords:
                # Best-effort prompt injection for providers that support `prompt`.
                form_data["prompt"] = "热词: " + ", ".join(hotwords)

            files = {
                "file": (upload_name, upload_data, upload_mime),
            }
            response = requests.post(
                self.endpoint,
                data=form_data,
                files=files,
                headers=headers,
                timeout=self.timeout_s,
            )
        else:
            # Legacy Recordian JSON protocol.
            payload = {
                "audio_base64": base64.b64encode(audio_data).decode("ascii"),
                "hotwords": hotwords,
            }
            headers["Content-Type"] = "application/json"
            response = requests.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )

        response.raise_for_status()
        body = response.json()

        text = str(body.get("text", body.get("result", ""))).strip()
        confidence = body.get("confidence")
        model_name = str(body.get("model", self.model_name or "cloud"))

        return ASRResult(
            text=text,
            confidence=confidence if isinstance(confidence, (int, float)) else None,
            english_ratio=_estimate_english_ratio(text),
            model_name=model_name,
            metadata={"raw": body, "source": "cloud_http"},
        )
