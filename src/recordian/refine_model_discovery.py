"""Model discovery for text-refine backend.

Fetches available models from an OpenAI-compatible /v1/models endpoint
so users can pick a refine model from a dropdown instead of typing it.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 8.0


def fetch_model_list(
    api_base: str,
    api_key: str | None = None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[str]:
    """Return model IDs from an OpenAI-compatible /v1/models endpoint.

    Args:
        api_base: Base URL, e.g. ``http://192.168.5.111/v1``.
        api_key: Optional bearer token.
        timeout_s: HTTP timeout in seconds.

    Returns:
        Sorted list of model IDs. Empty list on any error so the UI
        can fall back to a plain text entry.
    """
    base = api_base.rstrip("/")
    # Ensure /v1 path segment for OpenAI-compatible endpoints
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    url = f"{base}/models"
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("Model discovery HTTP %s: %s", exc.code, exc.reason)
        return []
    except urllib.error.URLError as exc:
        logger.warning("Model discovery failed: %s", exc.reason)
        return []
    except json.JSONDecodeError as exc:
        logger.warning("Model discovery bad JSON: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Model discovery error: %s", exc)
        return []

    data = payload.get("data", [])
    if not isinstance(data, list):
        logger.warning("Model discovery unexpected payload shape: %s", payload.keys())
        return []

    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())

    return sorted(set(models))


__all__ = ["fetch_model_list"]
