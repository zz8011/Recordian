import argparse
from pathlib import Path

from recordian.runtime_config import (
    DEFAULT_AUTO_LEXICON_DB,
    DEFAULT_OWNER_PROFILE,
    DEFAULT_SOUND_OFF_PATH,
    DEFAULT_SOUND_ON_PATH,
    apply_namespace_runtime_normalization,
    normalize_runtime_config,
)


def test_normalize_runtime_config_centralizes_compatibility_mappings() -> None:
    normalized = normalize_runtime_config(
        {
            "record_backend": "ffmpeg",
            "record_format": "mp3",
            "refine_provider": "llama.cpp",
            "commit_backend": "pynput",
            "notify_backend": "invalid",
            "wake_prefix": "嗨, 嘿 ,",
            "wake_name": [" 小二 ", "", "乐乐"],
            "wake_tokens_type": "char",
            "wake_owner_profile": "~/.config/recordian/profile.json",
            "wake_owner_sample": "~/owner.wav",
            "auto_lexicon_db": "~/lexicon.db",
        },
        include_sound_defaults=False,
        allow_auto_fallback_commit=True,
    )

    assert normalized["record_backend"] == "ffmpeg-pulse"
    assert normalized["record_format"] == "ogg"
    assert normalized["refine_provider"] == "llamacpp"
    assert normalized["commit_backend"] == "auto"
    assert normalized["notify_backend"] == "auto"
    assert normalized["wake_prefix"] == ["嗨", "嘿"]
    assert normalized["wake_name"] == ["小二", "乐乐"]
    assert normalized["wake_tokens_type"] == "ppinyin"
    assert normalized["wake_owner_profile"] == str(Path("~/.config/recordian/profile.json").expanduser())
    assert normalized["wake_owner_sample"] == str(Path("~/owner.wav").expanduser())
    assert normalized["auto_lexicon_db"] == str(Path("~/lexicon.db").expanduser())


def test_normalize_runtime_config_fills_sound_defaults_from_legacy_beep() -> None:
    normalized = normalize_runtime_config(
        {"wake_beep_path": "/tmp/legacy.mp3"},
        include_sound_defaults=True,
        allow_auto_fallback_commit=False,
    )

    assert normalized["sound_on_path"] == "/tmp/legacy.mp3"
    assert normalized["sound_off_path"] == "/tmp/legacy.mp3"

    normalized_without_legacy = normalize_runtime_config(
        {},
        include_sound_defaults=True,
        allow_auto_fallback_commit=False,
    )
    assert normalized_without_legacy["sound_on_path"] == DEFAULT_SOUND_ON_PATH
    assert normalized_without_legacy["sound_off_path"] == DEFAULT_SOUND_OFF_PATH
    assert normalized_without_legacy["wake_owner_profile"] == str(Path(DEFAULT_OWNER_PROFILE).expanduser())
    assert normalized_without_legacy["auto_lexicon_db"] == str(Path(DEFAULT_AUTO_LEXICON_DB).expanduser())


def test_apply_namespace_runtime_normalization_preserves_backend_only_values() -> None:
    args = argparse.Namespace(
        commit_backend="auto-fallback",
        record_backend="bogus",
        record_format="wav",
        refine_provider="bogus",
        notify_backend="stdout",
        wake_prefix="嘿",
        wake_name="小二",
        wake_tokens_type="unknown",
        wake_owner_profile="~/.config/recordian/owner.json",
        wake_owner_sample="",
        auto_lexicon_db="~/.config/recordian/lexicon.sqlite",
    )

    apply_namespace_runtime_normalization(args, allow_auto_fallback_commit=True)

    assert args.commit_backend == "auto-fallback"
    assert args.record_backend == "auto"
    assert args.record_format == "wav"
    assert args.refine_provider == "local"
    assert args.notify_backend == "stdout"
    assert args.wake_prefix == ["嘿"]
    assert args.wake_name == ["小二"]
    assert args.wake_tokens_type == "ppinyin"
