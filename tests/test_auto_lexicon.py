from pathlib import Path

from recordian.auto_lexicon import AutoLexicon, extract_terms


def test_extract_terms_handles_cn_and_en_tokens() -> None:
    terms = extract_terms("今天 开会 讨论 Recordian 和 OpenClaw 的计划")
    assert "开会" in terms
    assert "讨论" in terms
    assert "recordian" in terms
    assert "openclaw" in terms


def test_auto_lexicon_requires_min_accepts_before_injection(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(db_path=db_path, max_hotwords=20, min_accepts=2, max_terms=1000)
    try:
        lexicon.observe_accepted("Recordian 项目")
        hotwords = lexicon.compose_hotwords([])
        assert "recordian" not in hotwords

        lexicon.observe_accepted("Recordian 发布")
        hotwords = lexicon.compose_hotwords([])
        assert "recordian" in hotwords
    finally:
        lexicon.close()


def test_auto_lexicon_keeps_manual_hotwords_first(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(db_path=db_path, max_hotwords=5, min_accepts=1, max_terms=1000)
    try:
        lexicon.observe_accepted("recordian openclaw")
        merged = lexicon.compose_hotwords(["小二", "recordian"])
        assert merged[0] == "小二"
        assert merged[1] == "recordian"
        assert merged.count("recordian") == 1
    finally:
        lexicon.close()

