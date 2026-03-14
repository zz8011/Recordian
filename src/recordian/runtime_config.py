from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .voice_wake import normalize_tokens_type

DEFAULT_WAKE_PREFIX = ["嗨", "嘿"]
DEFAULT_WAKE_NAME = ["小二"]
DEFAULT_OWNER_PROFILE = "~/.config/recordian/owner_voice_profile.json"
DEFAULT_AUTO_LEXICON_DB = "~/.config/recordian/auto_lexicon.db"

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
    allowed = {"none", "auto", "wtype", "xdotool", "xdotool-clipboard", "stdout"}
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


def normalize_runtime_config(
    payload: Mapping[str, Any],
    *,
    include_sound_defaults: bool = False,
    allow_auto_fallback_commit: bool = True,
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
    normalized["wake_prefix"] = _normalize_string_list(
        normalized.get("wake_prefix", DEFAULT_WAKE_PREFIX),
        fallback=DEFAULT_WAKE_PREFIX,
    )
    normalized["wake_name"] = _normalize_string_list(
        normalized.get("wake_name", DEFAULT_WAKE_NAME),
        fallback=DEFAULT_WAKE_NAME,
    )
    normalized["wake_tokens_type"] = normalize_tokens_type(str(normalized.get("wake_tokens_type", "ppinyin")))
    normalized["wake_owner_profile"] = str(
        Path(str(normalized.get("wake_owner_profile", DEFAULT_OWNER_PROFILE)).strip() or DEFAULT_OWNER_PROFILE).expanduser()
    )
    owner_sample = str(normalized.get("wake_owner_sample", "")).strip()
    normalized["wake_owner_sample"] = str(Path(owner_sample).expanduser()) if owner_sample else ""
    normalized["auto_lexicon_db"] = str(
        Path(str(normalized.get("auto_lexicon_db", DEFAULT_AUTO_LEXICON_DB)).strip() or DEFAULT_AUTO_LEXICON_DB).expanduser()
    )
    if include_sound_defaults:
        legacy_beep = str(normalized.get("wake_beep_path", "")).strip()
        normalized["sound_on_path"] = str(
            normalized.get("sound_on_path", legacy_beep or DEFAULT_SOUND_ON_PATH)
        ).strip()
        normalized["sound_off_path"] = str(
            normalized.get("sound_off_path", legacy_beep or DEFAULT_SOUND_OFF_PATH)
        ).strip()
    else:
        if "sound_on_path" in normalized:
            normalized["sound_on_path"] = str(normalized.get("sound_on_path", "")).strip()
        if "sound_off_path" in normalized:
            normalized["sound_off_path"] = str(normalized.get("sound_off_path", "")).strip()
    return normalized


def apply_namespace_runtime_normalization(
    args: argparse.Namespace,
    *,
    include_sound_defaults: bool = False,
    allow_auto_fallback_commit: bool = True,
) -> None:
    normalized = normalize_runtime_config(
        vars(args),
        include_sound_defaults=include_sound_defaults,
        allow_auto_fallback_commit=allow_auto_fallback_commit,
    )
    for key, value in normalized.items():
        setattr(args, key, value)
