from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from recordian.runtime_config import normalize_runtime_config

logger = logging.getLogger(__name__)


def derive_openai_models_endpoint(endpoint: str) -> str | None:
    raw = str(endpoint).strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    path = parsed.path or ""
    suffix = "/audio/transcriptions"
    if path.lower().endswith(suffix):
        models_path = path[:-len(suffix)] + "/models"
        return urlunparse(parsed._replace(path=models_path, params="", query="", fragment=""))
    return None


def fetch_json_url(url: str, *, headers: Mapping[str, str], timeout_s: float) -> tuple[int, dict[str, Any]]:
    request = Request(url, headers=dict(headers), method="GET")
    with urlopen(request, timeout=timeout_s) as response:
        status = int(getattr(response, "status", response.getcode()))
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    return status, payload


def describe_provider_capabilities(provider: object) -> str:
    raw = getattr(provider, "capabilities", None)
    features: list[str] = []
    if raw is None:
        return "未知"
    if bool(getattr(raw, "supports_hotwords", False)):
        features.append("hotwords")
    if bool(getattr(raw, "supports_context", False)):
        features.append("context")
    if bool(getattr(raw, "supports_language_hint", False)):
        features.append("language_hint")
    if bool(getattr(raw, "supports_file_streaming", False)):
        features.append("file_streaming")
    if bool(getattr(raw, "supports_realtime", False)):
        features.append("realtime")
    return ", ".join(features) if features else "基础识别"


def create_asr_provider_for_diagnostics(config: Mapping[str, Any]) -> object:
    from argparse import Namespace

    from recordian.linux_dictate import create_provider

    return create_provider(Namespace(**dict(config)))


def collect_runtime_diagnostics(
    config: Mapping[str, Any],
    *,
    config_path: Path,
    backend_running: bool,
    backend_pid: int | None,
    fetch_json: Callable[[str], tuple[int, dict[str, Any]]] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def _add(label: str, status: str, detail: str) -> None:
        rows.append({"label": label, "status": status, "detail": detail})

    normalized = normalize_runtime_config(
        dict(config),
        include_sound_defaults=True,
        allow_auto_fallback_commit=False,
        config_base_dir=config_path.parent,
    )

    _add(
        "配置文件",
        "ok" if config_path.exists() else "warn",
        str(config_path),
    )
    if backend_running:
        detail = f"运行中 (PID {backend_pid})" if backend_pid is not None else "运行中"
        _add("后端进程", "ok", detail)
    else:
        _add("后端进程", "warn", "未运行")

    asr_provider = str(normalized.get("asr_provider", "qwen-asr")).strip() or "qwen-asr"
    _add("ASR 提供方", "info", asr_provider)
    try:
        provider = create_asr_provider_for_diagnostics(normalized)
    except Exception as exc:  # noqa: BLE001
        _add("ASR 能力", "warn", f"探测失败: {type(exc).__name__}: {exc}")
    else:
        _add("ASR 能力", "info", describe_provider_capabilities(provider))

    if asr_provider == "http-cloud":
        endpoint = str(normalized.get("asr_endpoint", "")).strip()
        model_name = str(normalized.get("qwen_model", "")).strip()
        _add("ASR 接口", "info", endpoint or "未配置")

        if endpoint:
            models_endpoint = derive_openai_models_endpoint(endpoint)
            if models_endpoint:
                headers = {"Accept": "application/json"}
                api_key = str(normalized.get("asr_api_key", "")).strip()
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                fetch = fetch_json or (lambda url: fetch_json_url(url, headers=headers, timeout_s=2.0))
                try:
                    status_code, payload = fetch(models_endpoint)
                    available_ids: list[str] = []
                    data = payload.get("data")
                    if isinstance(data, list):
                        for item in data:
                            if not isinstance(item, dict):
                                continue
                            model_id = item.get("id")
                            if isinstance(model_id, str) and model_id.strip():
                                available_ids.append(model_id.strip())
                    if status_code == 200:
                        if model_name and model_name in available_ids:
                            _add("ASR 模型", "ok", f"{model_name} (已匹配远端模型)")
                        elif model_name and "/" in model_name and model_name.rsplit("/", 1)[-1] in available_ids:
                            short_name = model_name.rsplit("/", 1)[-1]
                            _add("ASR 模型", "ok", f"{model_name} -> {short_name}")
                        elif model_name and available_ids:
                            _add("ASR 模型", "warn", f"{model_name} (远端可用: {', '.join(available_ids[:3])})")
                        elif available_ids:
                            _add("ASR 模型", "ok", ", ".join(available_ids[:3]))
                        else:
                            _add("ASR 模型", "warn", "接口可达，但未返回模型列表")
                    else:
                        _add("ASR 模型", "warn", f"{model_name or '未配置'} (模型探测返回 {status_code})")
                except HTTPError as exc:
                    _add("ASR 模型", "error", f"{model_name or '未配置'} (HTTP {exc.code})")
                except URLError as exc:
                    _add("ASR 模型", "error", f"{model_name or '未配置'} ({exc.reason})")
                except Exception as exc:  # noqa: BLE001
                    _add("ASR 模型", "error", f"{model_name or '未配置'} ({type(exc).__name__}: {exc})")
            else:
                _add("ASR 模型", "info", model_name or "未配置")
        else:
            _add("ASR 模型", "warn", model_name or "未配置")
    else:
        model_path = str(normalized.get("qwen_model", "")).strip()
        model_exists = bool(model_path) and Path(model_path).exists()
        _add("ASR 模型", "ok" if model_exists else "warn", model_path or "未配置")

    voice_wake_enabled = bool(normalized.get("enable_voice_wake", False))
    _add("语音唤醒", "ok" if voice_wake_enabled else "info", "已开启" if voice_wake_enabled else "已关闭")

    if voice_wake_enabled:
        owner_verify = bool(normalized.get("wake_owner_verify", False))
        _add("声纹校验", "ok" if owner_verify else "info", "已开启" if owner_verify else "已关闭")

        owner_profile = str(normalized.get("wake_owner_profile", "")).strip()
        owner_profile_exists = bool(owner_profile) and Path(owner_profile).exists()
        _add("声纹档案", "ok" if owner_profile_exists else "warn", owner_profile or "未配置")

    lexicon_db = str(normalized.get("auto_lexicon_db", "")).strip()
    lexicon_exists = bool(lexicon_db) and Path(lexicon_db).exists()
    _add("自动词库", "ok" if lexicon_exists else "info", lexicon_db or "未配置")

    return rows


def format_diagnostic_report(rows: list[dict[str, str]]) -> str:
    prefix_map = {
        "ok": "[OK]",
        "warn": "[WARN]",
        "error": "[ERROR]",
        "info": "[INFO]",
    }
    return "\n".join(
        f"{prefix_map.get(row.get('status', 'info'), '[INFO]')} {row.get('label', '')}: {row.get('detail', '')}"
        for row in rows
    )


__all__ = [
    "derive_openai_models_endpoint",
    "fetch_json_url",
    "describe_provider_capabilities",
    "create_asr_provider_for_diagnostics",
    "collect_runtime_diagnostics",
    "format_diagnostic_report",
]
