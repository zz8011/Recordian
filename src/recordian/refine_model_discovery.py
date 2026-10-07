"""Model discovery for text-refine backend.

Fetches available models from an OpenAI-compatible /v1/models endpoint
so users can pick a refine model from a dropdown instead of typing it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 8.0
MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer tokens must stay at the endpoint explicitly chosen by the user.
        return None


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

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=timeout_s) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                logger.warning("Model discovery response exceeds size limit")
                return []
            payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("Model discovery HTTP %s", exc.code)
        return []
    except urllib.error.URLError:
        logger.warning("Model discovery connection failed")
        return []
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Model discovery bad JSON")
        return []
    except Exception:
        logger.warning("Model discovery request failed")
        return []

    if not isinstance(payload, dict):
        logger.warning("Model discovery unexpected payload shape")
        return []
    data = payload.get("data", [])
    if not isinstance(data, list):
        logger.warning("Model discovery unexpected payload shape")
        return []

    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())

    return sorted(set(models))


__all__ = ["fetch_model_list"]
