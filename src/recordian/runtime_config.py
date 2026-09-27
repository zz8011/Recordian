from __future__ import annotations

import argparse
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .voice_wake import normalize_tokens_type

DEFAULT_WAKE_PREFIX = ["嗨", "嘿"]
DEFAULT_WAKE_NAME = ["小二"]
DEFAULT_OWNER_PROFILE = "~/.config/recordian/owner_voice_profile.json"
DEFAULT_AUTO_LEXICON_DB = "~/.config/recordian/auto_lexicon.db"
DEFAULT_REFINE_CAPTURE_PATH = "~/.local/share/recordian/refine-samples.jsonl"

# SemIf 候选纠错配置合同（实现逻辑在 hotword_corrector/semif 链路）：
# 默认关闭，端点留空，超时是有界正值。
DEFAULT_SEMIF_TIMEOUT_S = 0.12
MAX_SEMIF_TIMEOUT_S = 0.35
# Official Jev (SEMIF=0) measured about 0.4–0.6 s per call. 1.5 s covers one
# gated judgment with margin; 2 s is the hard cap. SemIf stays at 0.35 s.
DEFAULT_JEV_TIMEOUT_S = 1.5
MAX_JEV_TIMEOUT_S = 2.0
CORRECTION_PROVIDER_CHOICES = ("semif", "jev")
ASR_PROVIDER_CHOICES = ("qwen-asr", "http-cloud", "confucius-asr")


def normalize_correction_provider(value: object, *, fallback: str = "semif") -> str:
    """``semif`` (default, including old configs) or ``jev``."""
    return _normalize_choice(
        value,
        fallback=fallback if fallback in CORRECTION_PROVIDER_CHOICES else "semif",
        allowed=set(CORRECTION_PROVIDER_CHOICES),
    )


def normalize_jev_timeout_s(value: object, *, fallback: float = DEFAULT_JEV_TIMEOUT_S) -> float:
    """Finite positive Jev budget, clamped to ``MAX_JEV_TIMEOUT_S``."""
    if not math.isfinite(fallback) or fallback <= 0.0:
        fallback = DEFAULT_JEV_TIMEOUT_S
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(timeout) or timeout <= 0.0:
        return fallback
    return min(timeout, MAX_JEV_TIMEOUT_S)


def normalize_asr_provider(value: object, *, fallback: str = "qwen-asr") -> str:
    return _normalize_choice(
        value,
        fallback=fallback,
        allowed=set(ASR_PROVIDER_CHOICES),
    )


def normalize_semif_timeout_s(value: object, *, fallback: float = DEFAULT_SEMIF_TIMEOUT_S) -> float:
    # Central contract: a finite, positive, bounded timeout. Non-numbers,
    # non-finite values (NaN/±Inf) and non-positive numbers all fall back;
    # oversized positive finite values clamp to the max.
    if not math.isfinite(fallback) or fallback <= 0.0:
        fallback = DEFAULT_SEMIF_TIMEOUT_S
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(timeout) or timeout <= 0.0:
        return fallback
    return min(timeout, MAX_SEMIF_TIMEOUT_S)


def normalize_contextual_aliases(value: object) -> list[dict[str, str]]:
    """Normalize user-declared contextual aliases.

    Each entry is ``{"heard": "jeff", "word": "jev", "meaning": "软件工具"}``:
    the ASR-heard token, the intended word, and a truthful short meaning used
    as SemIf semantic-role context. Strings of the form
    ``heard→word::meaning`` (also ``->``/``=>``) are accepted. Invalid or
    incomplete entries drop out; the default is an empty list.
    """
    items: list[object]
    if isinstance(value, (str, bytes)):
        items = [part for part in re.split(r"[,，、;；\n]+", str(value)) if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        heard = word = meaning = ""
        if isinstance(item, Mapping):
            heard = str(item.get("heard", "")).strip()
            word = str(item.get("word", "")).strip()
            meaning = str(item.get("meaning", "")).strip()
        else:
            token = str(item).strip()
            pair, _, note = token.partition("::")
            meaning = note.strip()
            for arrow in ("→", "->", "=>"):
                if arrow in pair:
                    left, _, right = pair.partition(arrow)
                    heard, word = left.strip(), right.strip()
                    break
        if not heard or not word or not meaning or heard == word:
            continue
        if len(heard) > 32 or len(word) > 32 or len(meaning) > 64:
            continue
        key = (heard.casefold(), word)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"heard": heard, "word": word, "meaning": meaning})
        if len(normalized) >= 64:
            break
    return normalized

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"
DEFAULT_SOUND_ON_PATH = str(_ASSETS_DIR / "wake-on.mp3")
DEFAULT_SOUND_OFF_PATH = str(_ASSETS_DIR / "wake-off.mp3")


def _normalize_choice(
    value: object,
    *,
    fallback: str,
    allowed: set[str],
    aliases: Mapping[str, str] | None = None,
) -> str:
    token = str(value).strip()
    if aliases is not None:
        token = aliases.get(token, token)
    return token if token in allowed else fallback


def _normalize_string_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or list(fallback)
    if isinstance(value, list):
        items = [str(part).strip() for part in value if str(part).strip()]
        return items or list(fallback)
    return list(fallback)


def format_contextual_aliases(value: object) -> str:
    """Render normalized aliases as editable text: ``heard→word::meaning`` per line."""
    return "\n".join(
        f"{entry['heard']}→{entry['word']}::{entry['meaning']}"
        for entry in normalize_contextual_aliases(value)
    )


def normalize_record_backend(value: object, *, fallback: str = "auto") -> str:
    return _normalize_choice(
        value,
        fallback=fallback,
        allowed={"auto", "ffmpeg-pulse", "arecord"},
        aliases={"ffmpeg": "ffmpeg-pulse"},
    )


def normalize_record_format(value: object, *, fallback: str = "ogg") -> str:
    return _normalize_choice(
        str(value).lower(),
        fallback=fallback,
        allowed={"ogg", "wav"},
        aliases={"mp3": "ogg"},
    )


def normalize_refine_provider(value: object, *, fallback: str = "local") -> str:
    return _normalize_choice(
        value,
        fallback=fallback,
        allowed={"local", "cloud", "llamacpp"},
        aliases={"llama.cpp": "llamacpp"},
    )


def normalize_commit_backend(
    value: object,
    *,
    fallback: str = "auto",
    allow_auto_fallback: bool = True,
) -> str:
    allowed = {"none", "auto", "fcitx", "wtype", "xdotool", "xdotool-clipboard", "stdout"}
    if allow_auto_fallback:
        allowed.add("auto-fallback")
    return _normalize_choice(
        value,
        fallback=fallback,
        allowed=allowed,
        aliases={"pynput": "auto"},
    )


def normalize_notify_backend(value: object, *, fallback: str = "auto") -> str:
    return _normalize_choice(
        value,
        fallback=fallback,
        allowed={"none", "auto", "notify-send", "stdout"},
    )


def _normalize_path_string(value: object, *, base_dir: Path | None = None) -> str:
    raw = str(value).strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    if base_dir is not None:
        return str((base_dir / path).resolve())
    return str(path)


def normalize_runtime_config(
    payload: Mapping[str, Any],
    *,
    include_sound_defaults: bool = False,
    allow_auto_fallback_commit: bool = True,
    config_base_dir: Path | None = None,
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["record_backend"] = normalize_record_backend(normalized.get("record_backend", "auto"))
    normalized["record_format"] = normalize_record_format(normalized.get("record_format", "ogg"))
    normalized["refine_provider"] = normalize_refine_provider(normalized.get("refine_provider", "local"))
    normalized["commit_backend"] = normalize_commit_backend(
        normalized.get("commit_backend", "auto"),
        allow_auto_fallback=allow_auto_fallback_commit,
    )
    normalized["notify_backend"] = normalize_notify_backend(normalized.get("notify_backend", "auto"))
    normalized["enable_streaming_commit"] = bool(normalized.get("enable_streaming_commit", False))
    normalized["asr_provider"] = normalize_asr_provider(normalized.get("asr_provider", "qwen-asr"))
    normalized["asr_realtime_endpoint"] = str(normalized.get("asr_realtime_endpoint") or "").strip()
    normalized["enable_semif_correction"] = bool(normalized.get("enable_semif_correction", False))
    normalized["correction_provider"] = normalize_correction_provider(normalized.get("correction_provider", "semif"))
    normalized["semif_endpoint"] = str(normalized.get("semif_endpoint") or "").strip()
    normalized["semif_timeout_s"] = normalize_semif_timeout_s(normalized.get("semif_timeout_s", DEFAULT_SEMIF_TIMEOUT_S))
    normalized["jev_timeout_s"] = normalize_jev_timeout_s(normalized.get("jev_timeout_s", DEFAULT_JEV_TIMEOUT_S))
    normalized["contextual_aliases"] = normalize_contextual_aliases(normalized.get("contextual_aliases", []))
    normalized["wake_prefix"] = _normalize_string_list(
        normalized.get("wake_prefix", DEFAULT_WAKE_PREFIX),
        fallback=DEFAULT_WAKE_PREFIX,
    )
    normalized["wake_name"] = _normalize_string_list(
        normalized.get("wake_name", DEFAULT_WAKE_NAME),
        fallback=DEFAULT_WAKE_NAME,
    )
    normalized["wake_tokens_type"] = normalize_tokens_type(str(normalized.get("wake_tokens_type", "ppinyin")))
    normalized["wake_owner_profile"] = _normalize_path_string(
        str(normalized.get("wake_owner_profile", DEFAULT_OWNER_PROFILE)).strip() or DEFAULT_OWNER_PROFILE,
        base_dir=config_base_dir,
    )
    owner_sample = str(normalized.get("wake_owner_sample", "")).strip()
    normalized["wake_owner_sample"] = _normalize_path_string(owner_sample, base_dir=config_base_dir) if owner_sample else ""
    normalized["auto_lexicon_db"] = _normalize_path_string(
        str(normalized.get("auto_lexicon_db", DEFAULT_AUTO_LEXICON_DB)).strip() or DEFAULT_AUTO_LEXICON_DB,
        base_dir=config_base_dir,
    )
    if "capture_refine_samples_path" in normalized:
        normalized["capture_refine_samples_path"] = _normalize_path_string(
            normalized.get("capture_refine_samples_path", DEFAULT_REFINE_CAPTURE_PATH),
            base_dir=config_base_dir,
        )
    normalized["deskflow_active_screen_path"] = _normalize_path_string(
        normalized.get("deskflow_active_screen_path", "~/.local/state/deskflow/active_screen.json"),
        base_dir=config_base_dir,
    )
    if "deskflow_log_path" in normalized:
        normalized["deskflow_log_path"] = _normalize_path_string(
            normalized.get("deskflow_log_path", ""),
            base_dir=config_base_dir,
        )
    if include_sound_defaults:
        legacy_beep = str(normalized.get("wake_beep_path", "")).strip()
        normalized["sound_on_path"] = _normalize_path_string(
            normalized.get("sound_on_path", legacy_beep or DEFAULT_SOUND_ON_PATH),
            base_dir=_PROJECT_ROOT,
        )
        normalized["sound_off_path"] = _normalize_path_string(
            normalized.get("sound_off_path", legacy_beep or DEFAULT_SOUND_OFF_PATH),
            base_dir=_PROJECT_ROOT,
        )
    else:
        if "sound_on_path" in normalized:
            normalized["sound_on_path"] = _normalize_path_string(normalized.get("sound_on_path", ""), base_dir=_PROJECT_ROOT)
        if "sound_off_path" in normalized:
            normalized["sound_off_path"] = _normalize_path_string(normalized.get("sound_off_path", ""), base_dir=_PROJECT_ROOT)

    for key in ("wake_encoder", "wake_decoder", "wake_joiner", "wake_tokens", "wake_keywords_file"):
        if key in normalized:
            normalized[key] = _normalize_path_string(normalized.get(key, ""), base_dir=_PROJECT_ROOT)
    return normalized


def apply_namespace_runtime_normalization(
    args: argparse.Namespace,
    *,
    include_sound_defaults: bool = False,
    allow_auto_fallback_commit: bool = True,
    config_base_dir: Path | None = None,
) -> None:
    normalized = normalize_runtime_config(
        vars(args),
        include_sound_defaults=include_sound_defaults,
        allow_auto_fallback_commit=allow_auto_fallback_commit,
        config_base_dir=config_base_dir,
    )
    for key, value in normalized.items():
        setattr(args, key, value)
