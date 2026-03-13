from __future__ import annotations

import argparse
import types
from pathlib import Path

from recordian.voice_wake import (
    DEFAULT_WAKE_KEYWORD_THRESHOLD,
    DEFAULT_WAKE_NUM_THREADS,
    build_wake_phrases,
    ensure_keywords_file,
    make_wake_model_config,
    make_wake_runtime_config,
    normalize_tokens_type,
)


def test_build_wake_phrases_cross_product() -> None:
    phrases = build_wake_phrases(["嗨", "嘿"], ["小二", "乐乐"])
    assert phrases == ["嗨小二", "嗨乐乐", "嘿小二", "嘿乐乐"]


def test_build_wake_phrases_dedup_and_strip() -> None:
    phrases = build_wake_phrases([" 嗨 ", "嗨", ""], [" 小二 ", ""])
    assert phrases == ["嗨小二"]


def test_build_wake_phrases_auto_name_variants() -> None:
    phrases = build_wake_phrases(["嘿"], ["小二"], auto_name_variants=True)
    assert "嘿小二" in phrases
    assert "嘿小耳" not in phrases


def test_build_wake_phrases_auto_prefix_variants_and_name_only() -> None:
    phrases = build_wake_phrases(
        ["嘿"],
        ["小二"],
        auto_name_variants=False,
        auto_prefix_variants=True,
        allow_name_only=True,
    )
    assert "小二" in phrases
    assert "嘿小二" in phrases
    assert "嗨小二" in phrases
    assert "黑小二" in phrases


def test_make_wake_runtime_config() -> None:
    args = argparse.Namespace(
        enable_voice_wake=True,
        wake_prefix=["嗨"],
        wake_name=["小二"],
        wake_cooldown_s=3.0,
        wake_keyword_score=1.6,
        wake_keyword_threshold=0.24,
        wake_auto_name_variants=True,
        wake_auto_prefix_variants=True,
        wake_allow_name_only=True,
        wake_owner_verify=True,
        wake_owner_profile="/tmp/owner.profile.json",
        wake_owner_sample="/tmp/owner.wav",
        wake_owner_threshold=0.78,
        wake_owner_window_s=1.8,
    )
    cfg = make_wake_runtime_config(args)
    assert cfg.enabled is True
    assert cfg.prefixes == ["嗨"]
    assert cfg.names == ["小二"]
    assert cfg.cooldown_s == 3.0
    assert cfg.keyword_score == 1.6
    assert cfg.keyword_threshold == 0.24
    assert cfg.auto_name_variants is True
    assert cfg.auto_prefix_variants is True
    assert cfg.allow_name_only is True
    assert cfg.owner_verify_enabled is True
    assert cfg.owner_profile_path == "/tmp/owner.profile.json"
    assert cfg.owner_sample_path == "/tmp/owner.wav"
    assert cfg.owner_threshold == 0.78
    assert cfg.owner_window_s == 1.8


def test_make_wake_runtime_config_accepts_csv_string() -> None:
    args = argparse.Namespace(
        enable_voice_wake=True,
        wake_prefix="嗨,嘿",
        wake_name="小二,乐乐",
        wake_cooldown_s=3.0,
        wake_keyword_score=1.5,
        wake_keyword_threshold=0.25,
    )
    cfg = make_wake_runtime_config(args)
    assert cfg.prefixes == ["嗨", "嘿"]
    assert cfg.names == ["小二", "乐乐"]
    assert cfg.auto_prefix_variants is True
    assert cfg.allow_name_only is True
    assert cfg.owner_verify_enabled is False


def test_make_wake_model_config() -> None:
    args = argparse.Namespace(
        wake_encoder="/tmp/e.onnx",
        wake_decoder="/tmp/d.onnx",
        wake_joiner="/tmp/j.onnx",
        wake_tokens="/tmp/tokens.txt",
        wake_provider="cpu",
        wake_num_threads=2,
        wake_sample_rate=16000,
        wake_tokens_type="ppinyin",
        wake_keywords_file="",
    )
    cfg = make_wake_model_config(args)
    assert cfg.encoder.endswith("e.onnx")
    assert cfg.tokens_type == "ppinyin"


def test_make_wake_model_config_normalizes_legacy_char() -> None:
    args = argparse.Namespace(
        wake_encoder="/tmp/e.onnx",
        wake_decoder="/tmp/d.onnx",
        wake_joiner="/tmp/j.onnx",
        wake_tokens="/tmp/tokens.txt",
        wake_provider="cpu",
        wake_num_threads=2,
        wake_sample_rate=16000,
        wake_tokens_type="char",
        wake_keywords_file="",
    )
    cfg = make_wake_model_config(args)
    assert cfg.tokens_type == "ppinyin"


def test_make_wake_configs_use_shared_defaults() -> None:
    args = argparse.Namespace()

    model_cfg = make_wake_model_config(args)
    runtime_cfg = make_wake_runtime_config(args)

    assert model_cfg.num_threads == DEFAULT_WAKE_NUM_THREADS
    assert runtime_cfg.keyword_threshold == DEFAULT_WAKE_KEYWORD_THRESHOLD


def test_normalize_tokens_type() -> None:
    assert normalize_tokens_type("char") == "ppinyin"
    assert normalize_tokens_type("cjkchar") == "ppinyin"
    assert normalize_tokens_type("ppinyin") == "ppinyin"
    assert normalize_tokens_type("unknown") == "ppinyin"


def test_ensure_keywords_file_runs_converter(monkeypatch, tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("dummy", encoding="utf-8")

    fake_utils = types.ModuleType("sherpa_onnx.utils")

    def _fake_text2token(*, texts, tokens, tokens_type):  # noqa: ANN001
        assert texts == ["嗨小二"]
        assert tokens.endswith("tokens.txt")
        assert tokens_type == "ppinyin"
        return [["h", "āi", "x", "iǎo", "èr"]]

    fake_utils.text2token = _fake_text2token  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "sherpa_onnx.utils", fake_utils)
    monkeypatch.setattr("recordian.voice_wake.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("should not call subprocess")))

    out = ensure_keywords_file(
        phrases=["嗨小二"],
        tokens_path=tokens,
        tokens_type="ppinyin",
        score=1.5,
        threshold=0.25,
        cache_dir=tmp_path / "cache",
    )

    assert out.exists()
    assert "@嗨小二" in out.read_text(encoding="utf-8")
    raw = (tmp_path / "cache" / "keywords_raw.txt").read_text(encoding="utf-8")
    assert "嗨小二 @嗨小二 :1.50 #0.25" in raw


def test_ensure_keywords_file_rebuilds_invalid_cached_output(monkeypatch, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    raw_path = cache / "keywords_raw.txt"
    out_path = cache / "keywords.txt"
    raw_path.write_text("嗨小二 @嗨小二 :1.50 #0.25\n", encoding="utf-8")
    out_path.write_text("", encoding="utf-8")

    tokens = tmp_path / "tokens.txt"
    tokens.write_text("dummy", encoding="utf-8")

    fake_utils = types.ModuleType("sherpa_onnx.utils")

    def _fake_text2token(*, texts, tokens, tokens_type):  # noqa: ANN001
        return [["h", "āi", "x", "iǎo", "èr"]]

    fake_utils.text2token = _fake_text2token  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "sherpa_onnx.utils", fake_utils)
    monkeypatch.setattr("recordian.voice_wake.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("should not call subprocess")))

    out = ensure_keywords_file(
        phrases=["嗨小二"],
        tokens_path=tokens,
        tokens_type="ppinyin",
        score=1.5,
        threshold=0.25,
        cache_dir=cache,
    )

    assert out == out_path
    assert out.read_text(encoding="utf-8").strip() == "h āi x iǎo èr @嗨小二 :1.50 #0.25"


def test_ensure_keywords_file_expands_tone_variants_generically(monkeypatch, tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text(
        "\n".join(
            [
                "<blk> 0",
                "h 1",
                "ēi 2",
                "x 3",
                "iǎo 4",
                "iào 5",
                "èr 6",
                "ěr 7",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fake_utils = types.ModuleType("sherpa_onnx.utils")

    def _fake_text2token(*, texts, tokens, tokens_type):  # noqa: ANN001
        assert texts == ["嘿小二"]
        assert tokens_type == "ppinyin"
        return [["h", "ēi", "x", "iǎo", "èr"]]

    fake_utils.text2token = _fake_text2token  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "sherpa_onnx.utils", fake_utils)

    out = ensure_keywords_file(
        phrases=["嘿小二"],
        tokens_path=tokens,
        tokens_type="ppinyin",
        score=1.5,
        threshold=0.25,
        cache_dir=tmp_path / "cache",
        auto_tone_variants=True,
    )

    lines = out.read_text(encoding="utf-8").splitlines()
    assert "h ēi x iǎo èr @嘿小二 :1.50 #0.25" in lines
    assert "h ēi x iào èr @嘿小二 :1.50 #0.25" in lines
    assert "h ēi x iǎo ěr @嘿小二 :1.50 #0.25" in lines
