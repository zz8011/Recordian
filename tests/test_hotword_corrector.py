import pytest

from recordian.hotword_corrector import correct_hotwords


def test_ascii_case_and_spacing_variants_use_canonical_form() -> None:
    text = "回去开 CodeX 或者用 open claw 来开发"
    corrected, changes = correct_hotwords(text, ["OPENCLAW", "OpenClaw", "open claw", "Codex"])
    assert "Codex" in corrected
    assert "OpenClaw" in corrected
    assert "CodeX" not in corrected
    assert "open claw" not in corrected
    assert ("CodeX", "Codex") in changes
    assert ("open claw", "OpenClaw") in changes


def test_ascii_edit_distance_one_corrects_near_miss() -> None:
    corrected, changes = correct_hotwords("把代码提交到 Githup 上", ["github"])
    assert corrected == "把代码提交到 github 上"
    assert changes == [("Githup", "github")]


def test_ascii_edit_distance_never_applies_to_short_keys() -> None:
    # "codes" is 1 edit away from "codex", but short hotwords (< 6 compact
    # chars) only get case/spacing variant matching — common English words
    # must not be rewritten.
    corrected, changes = correct_hotwords("check the codes first", ["Codex"])
    assert corrected == "check the codes first"
    assert changes == []


def test_ascii_edit_distance_two_is_not_allowed_by_default() -> None:
    # cloud -> claude is 2 edits; must stay untouched so plain English "cloud"
    # keeps its meaning.
    corrected, changes = correct_hotwords("deploy to the cloud tonight", ["Claude"])
    assert corrected == "deploy to the cloud tonight"
    assert changes == []


def test_ascii_edit_distance_can_be_disabled() -> None:
    corrected, changes = correct_hotwords("把代码提交到 Githup 上, 用 CodeX", ["github", "Codex"], max_ascii_edits=0)
    assert "Githup" in corrected
    assert "CodeX" not in corrected
    assert changes == [("CodeX", "Codex")]


def test_no_changes_when_text_already_canonical() -> None:
    corrected, changes = correct_hotwords("用 Codex 和 Claude 开发 Recordian", ["Codex", "Claude", "Recordian"])
    assert changes == []
    assert corrected == "用 Codex 和 Claude 开发 Recordian"


def test_cjk_homophone_correction() -> None:
    pytest.importorskip("pypinyin")
    corrected, changes = correct_hotwords("张征和张艳东讨论这个问题", ["张拯", "张彦东"])
    assert "张拯" in corrected
    assert "张彦东" in corrected
    assert ("张征", "张拯") in changes
    assert ("张艳东", "张彦东") in changes


def test_cjk_correct_term_is_not_rewritten() -> None:
    pytest.importorskip("pypinyin")
    corrected, changes = correct_hotwords("我跟张拯确认了方案", ["张拯"])
    assert corrected == "我跟张拯确认了方案"
    assert changes == []


def test_empty_input_and_empty_hotwords() -> None:
    assert correct_hotwords("", ["Codex"]) == ("", [])
    assert correct_hotwords("你好世界", []) == ("你好世界", [])


def test_parse_user_lexicon_splits_commas_and_replacements() -> None:
    from recordian.hotword_corrector import parse_user_lexicon

    hotwords, replacements = parse_user_lexicon(
        "Recordian, Claude\n张征 → 张拯\nCodeX -> Codex\n请把会议内容整理成要点。"
    )
    assert "Recordian" in hotwords
    assert "Claude" in hotwords
    assert "Codex" in hotwords
    assert "张拯" in hotwords
    assert ("张征", "张拯") in replacements
    assert ("CodeX", "Codex") in replacements
    assert "请把会议内容整理成要点。" not in hotwords


def test_parse_user_lexicon_ignores_long_prompt_sentences() -> None:
    from recordian.hotword_corrector import parse_user_lexicon

    hotwords, replacements = parse_user_lexicon("这是一段给识别模型的上下文说明文字没有逗号")
    assert hotwords == []
    assert replacements == []


def test_explicit_replacements_win_over_heuristic() -> None:
    corrected, changes = correct_hotwords(
        "回去开 CodeX 找张征",
        ["Codex", "张拯"],
        replacements=[("CodeX", "Codex"), ("张征", "张拯")],
    )
    assert "Codex" in corrected
    assert "张拯" in corrected
    assert "CodeX" not in corrected
    assert ("CodeX", "Codex") in changes
    assert ("张征", "张拯") in changes


def test_ascii_replacement_does_not_rewrite_inside_longer_word() -> None:
    from recordian.hotword_corrector import apply_lexicon_replacements

    corrected, changes = apply_lexicon_replacements("category catalog", [("cat", "dog")])
    assert corrected == "category catalog"
    assert changes == []


def test_lexicon_from_args_merges_context_and_cli_hotwords() -> None:
    import argparse

    from recordian.hotword_corrector import compose_effective_hotwords, lexicon_from_args

    args = argparse.Namespace(
        hotword=["OpenClaw"],
        asr_context="Recordian, 张征 → 张拯",
        hotword_replacement=["Githup->GitHub"],
    )
    hotwords, replacements = lexicon_from_args(args)
    assert "OpenClaw" in hotwords
    assert "Recordian" in hotwords
    assert "张拯" in hotwords
    assert "GitHub" in hotwords
    assert ("张征", "张拯") in replacements
    assert ("Githup", "GitHub") in replacements
    assert compose_effective_hotwords(args) == hotwords
