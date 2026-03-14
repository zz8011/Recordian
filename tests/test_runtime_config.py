import argparse
from pathlib import Path

import recordian.runtime_config as runtime_config
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


def test_normalize_runtime_config_resolves_relative_paths_from_stable_bases(tmp_path: Path) -> None:
    normalized = normalize_runtime_config(
        {
            "wake_owner_profile": "profiles/owner.json",
            "wake_owner_sample": "samples/owner.wav",
            "auto_lexicon_db": "db/lexicon.sqlite",
            "sound_on_path": "assets/custom-on.mp3",
            "sound_off_path": "assets/custom-off.mp3",
            "wake_encoder": "models/wake/encoder.onnx",
            "wake_decoder": "models/wake/decoder.onnx",
            "wake_joiner": "models/wake/joiner.onnx",
            "wake_tokens": "models/wake/tokens.txt",
        },
        include_sound_defaults=False,
        allow_auto_fallback_commit=False,
        config_base_dir=tmp_path,
    )

    project_root = Path(runtime_config.__file__).resolve().parent.parent.parent

    assert normalized["wake_owner_profile"] == str((tmp_path / "profiles/owner.json").resolve())
    assert normalized["wake_owner_sample"] == str((tmp_path / "samples/owner.wav").resolve())
    assert normalized["auto_lexicon_db"] == str((tmp_path / "db/lexicon.sqlite").resolve())
    assert normalized["sound_on_path"] == str((project_root / "assets/custom-on.mp3").resolve())
    assert normalized["sound_off_path"] == str((project_root / "assets/custom-off.mp3").resolve())
    assert normalized["wake_encoder"] == str((project_root / "models/wake/encoder.onnx").resolve())
    assert normalized["wake_decoder"] == str((project_root / "models/wake/decoder.onnx").resolve())
    assert normalized["wake_joiner"] == str((project_root / "models/wake/joiner.onnx").resolve())
    assert normalized["wake_tokens"] == str((project_root / "models/wake/tokens.txt").resolve())


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
