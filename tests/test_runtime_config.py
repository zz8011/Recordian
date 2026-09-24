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
    assert normalized["enable_streaming_commit"] is False
    assert normalized["asr_realtime_endpoint"] == ""


def test_normalize_runtime_config_defaults_streaming_off() -> None:
    normalized = normalize_runtime_config(
        {"enable_streaming_commit": True, "asr_realtime_endpoint": "  http://127.0.0.1:40002  "},
        include_sound_defaults=False,
        allow_auto_fallback_commit=True,
    )
    assert normalized["enable_streaming_commit"] is True
    assert normalized["asr_realtime_endpoint"] == "http://127.0.0.1:40002"

    defaults = normalize_runtime_config({}, include_sound_defaults=False)
    assert defaults["enable_streaming_commit"] is False
    assert defaults["asr_realtime_endpoint"] == ""


def test_normalize_runtime_config_semif_contract() -> None:
    defaults = normalize_runtime_config({}, include_sound_defaults=False)
    assert defaults["enable_semif_correction"] is False
    assert defaults["semif_endpoint"] == ""
    assert defaults["semif_timeout_s"] == 0.12

    enabled = normalize_runtime_config(
        {
            "enable_semif_correction": True,
            "semif_endpoint": "  http://192.168.5.111:42032/v1/systemone  ",
            "semif_timeout_s": 0.2,
        },
        include_sound_defaults=False,
    )
    assert enabled["enable_semif_correction"] is True
    assert enabled["semif_endpoint"] == "http://192.168.5.111:42032/v1/systemone"
    assert enabled["semif_timeout_s"] == 0.2


def test_normalize_semif_timeout_bounds() -> None:
    from recordian.runtime_config import normalize_semif_timeout_s

    assert normalize_semif_timeout_s(0.5) == 0.35  # capped
    assert normalize_semif_timeout_s(0.35) == 0.35  # exact max passes through
    assert normalize_semif_timeout_s(0.0) == 0.12  # non-positive -> default
    assert normalize_semif_timeout_s(-1.0) == 0.12
    assert normalize_semif_timeout_s("not-a-number") == 0.12
    assert normalize_semif_timeout_s(None) == 0.12
    assert normalize_semif_timeout_s("0.2") == 0.2


def test_normalize_semif_timeout_rejects_non_finite() -> None:
    import math

    from recordian.runtime_config import normalize_semif_timeout_s

    # NaN slipped through before: NaN <= 0 is False and min(NaN, 0.35) is NaN,
    # violating the finite-positive bounded contract.
    for nasty in (float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf", math.nan, math.inf):
        result = normalize_semif_timeout_s(nasty)
        assert result == 0.12, f"{nasty!r} -> {result!r}"
        assert math.isfinite(result) and result > 0.0


def test_normalize_semif_timeout_malformed_fallback_uses_default() -> None:
    from recordian.runtime_config import DEFAULT_SEMIF_TIMEOUT_S, normalize_semif_timeout_s

    # A malformed explicit fallback must not leak NaN/non-positive either.
    assert normalize_semif_timeout_s(0.2, fallback=float("nan")) == 0.2
    assert normalize_semif_timeout_s("bad", fallback=float("nan")) == DEFAULT_SEMIF_TIMEOUT_S
    assert normalize_semif_timeout_s("bad", fallback=-1.0) == DEFAULT_SEMIF_TIMEOUT_S


def test_normalize_asr_provider_choices() -> None:
    normalized = normalize_runtime_config({"asr_provider": "confucius-asr"}, include_sound_defaults=False)
    assert normalized["asr_provider"] == "confucius-asr"

    fallback = normalize_runtime_config({"asr_provider": "gpt-cloud"}, include_sound_defaults=False)
    assert fallback["asr_provider"] == "qwen-asr"


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
