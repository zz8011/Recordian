from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path
from shutil import which
from urllib.parse import urlparse, urlunparse

from ..models import ASRResult
from .base import ASRProvider, _estimate_english_ratio


class _HttpCloudRealtimeSession:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_s: float,
        model_name: str,
        language: str,
        context: str,
        chunk_size_sec: float,
        unfixed_chunk_num: int,
        unfixed_token_num: int,
    ) -> None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "requests library is required for HttpCloudProvider. Install with: pip install requests"
            ) from exc

        self._session = requests.Session()
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._model_name = model_name
        self._language = language
        self._context = context
        self._chunk_size_sec = chunk_size_sec
        self._unfixed_chunk_num = unfixed_chunk_num
        self._unfixed_token_num = unfixed_token_num
        self._session_id = ""
        self._started_at = 0.0

    def _headers(self, *, content_type: str) -> dict[str, str]:
        headers = {"Content-Type": content_type}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def start(self) -> dict[str, object]:
        payload = {
            "model": self._model_name,
            "language": self._language or None,
            "context": self._context,
            "chunk_size_sec": self._chunk_size_sec,
            "unfixed_chunk_num": self._unfixed_chunk_num,
            "unfixed_token_num": self._unfixed_token_num,
        }
        response = self._session.post(
            f"{self._base_url}/api/start",
            headers=self._headers(content_type="application/json"),
            json=payload,
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        self._session_id = str(body.get("session_id", "")).strip()
        if not self._session_id:
            raise RuntimeError("realtime_asr_missing_session_id")
        self._started_at = time.perf_counter()
        return body

    def push_audio(self, payload: bytes) -> dict[str, object]:
        if not self._session_id:
            raise RuntimeError("realtime_asr_session_not_started")
        response = self._session.post(
            f"{self._base_url}/api/chunk",
            params={"session_id": self._session_id},
            headers=self._headers(content_type="application/octet-stream"),
            data=payload,
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        return response.json()

    def finish(self) -> ASRResult:
        if not self._session_id:
            raise RuntimeError("realtime_asr_session_not_started")
        response = self._session.post(
            f"{self._base_url}/api/finish",
            params={"session_id": self._session_id},
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        text = str(body.get("text", ""))
        return ASRResult(
            text=text,
            english_ratio=_estimate_english_ratio(text),
            model_name=str(body.get("model", self._model_name)),
            metadata={
                "language": body.get("language"),
                "latency_seconds": body.get("latency_seconds"),
                "realtime": True,
            },
        )

    def cancel(self) -> None:
        if not self._session_id:
            return
        try:
            self._session.delete(
                f"{self._base_url}/api/session",
                params={"session_id": self._session_id},
                timeout=min(10.0, self._timeout_s),
            )
        except Exception:
            pass

    @property
    def elapsed_ms(self) -> float:
        if self._started_at <= 0.0:
            return 0.0
        return (time.perf_counter() - self._started_at) * 1000


class HttpCloudProvider(ASRProvider):
    """Generic HTTP provider."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        model_name: str = "",
        language: str = "",
        realtime_endpoint: str = "",
        realtime_chunk_size_sec: float = 0.5,
        realtime_unfixed_chunk_num: int = 4,
        realtime_unfixed_token_num: int = 5,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.model_name = model_name.strip() or "Qwen/Qwen3-ASR-1.7B"
        self.language = language.strip()
        self.realtime_endpoint = realtime_endpoint.strip()
        self.realtime_chunk_size_sec = max(0.1, float(realtime_chunk_size_sec))
        self.realtime_unfixed_chunk_num = max(0, int(realtime_unfixed_chunk_num))
        self.realtime_unfixed_token_num = max(0, int(realtime_unfixed_token_num))
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

    def _resolve_realtime_base_url(self) -> str:
        return self.realtime_endpoint.strip().rstrip("/")

    def supports_realtime_transcription(self) -> bool:
        return bool(self.realtime_endpoint.strip())

    def start_realtime_session(self, *, hotwords: list[str]):
        context = ""
        if hotwords:
            context = "热词: " + ", ".join(hotwords)
        session = _HttpCloudRealtimeSession(
            base_url=self._resolve_realtime_base_url(),
            api_key=self.api_key,
            timeout_s=self.timeout_s,
            model_name=self.model_name,
            language=self.language,
            context=context,
            chunk_size_sec=self.realtime_chunk_size_sec,
            unfixed_chunk_num=self.realtime_unfixed_chunk_num,
            unfixed_token_num=self.realtime_unfixed_token_num,
        )
        session.start()
        return session

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

    def _build_headers(self, *, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_openai_form_data(self, requests_module, headers: dict[str, str], *, hotwords: list[str]) -> dict[str, str]:  # noqa: ANN001
        form_data: dict[str, str] = {
            "model": self._resolve_openai_model_name(requests_module, headers),
        }
        if self.language and self.language.lower() != "auto":
            lang_map = {"chinese": "zh", "english": "en"}
            normalized = lang_map.get(self.language.lower(), self.language)
            form_data["language"] = normalized
        if hotwords:
            form_data["prompt"] = "热词: " + ", ".join(hotwords)
        return form_data

    def transcribe_file_stream(self, wav_path: Path, *, hotwords: list[str]):
        if not wav_path.exists():
            raise FileNotFoundError(wav_path)
        if not self._is_openai_transcription_endpoint():
            raise NotImplementedError("streaming transcription only supports OpenAI-compatible endpoints")

        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "requests library is required for HttpCloudProvider. Install with: pip install requests"
            ) from exc

        headers = self._build_headers(accept="text/event-stream")
        upload_data, upload_name, upload_mime = self._prepare_openai_audio_file(wav_path)
        form_data = self._build_openai_form_data(requests, headers, hotwords=hotwords)
        form_data["stream"] = "true"
        files = {
            "file": (upload_name, upload_data, upload_mime),
        }
        response = requests.post(
            self.endpoint,
            data=form_data,
            files=files,
            headers=headers,
            timeout=self.timeout_s,
            stream=True,
        )
        response.raise_for_status()

        started = False
        buffered = ""
        asr_tag = "<asr_text>"
        end_tag = "</asr_text>"

        for raw_line in response.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta", {})
            if not isinstance(delta, dict):
                continue
            chunk = str(delta.get("content", ""))
            if not chunk:
                continue
            buffered += chunk
            if not started:
                idx = buffered.find(asr_tag)
                if idx != -1:
                    started = True
                    buffered = buffered[idx + len(asr_tag):]
                elif len(buffered) > 64:
                    started = True
            if not started:
                continue
            while True:
                end_idx = buffered.find(end_tag)
                if end_idx == -1:
                    break
                current = buffered[:end_idx]
                if current:
                    yield current
                buffered = buffered[end_idx + len(end_tag):]
                started = False
            if started and buffered:
                yield buffered
                buffered = ""

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
        headers = self._build_headers(accept="application/json")

        if self._is_openai_transcription_endpoint():
            upload_data, upload_name, upload_mime = self._prepare_openai_audio_file(wav_path)
            form_data = self._build_openai_form_data(requests, headers, hotwords=hotwords)
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
            payload = {
                "audio_base64": base64.b64encode(audio_data).decode("utf-8"),
                "hotwords": hotwords,
            }
            response = requests.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )

        response.raise_for_status()
        data = response.json()
        text = str(data.get("text", ""))
        return ASRResult(
            text=text,
            confidence=data.get("confidence"),
            english_ratio=_estimate_english_ratio(text),
            model_name=str(data.get("model", self.model_name)),
            metadata={k: v for k, v in data.items() if k not in {"text", "confidence", "model"}},
        )
