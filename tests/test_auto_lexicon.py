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


def test_extract_terms_rejects_spoken_fragments() -> None:
    terms = extract_terms("我觉得这个项目是不是应该帮我看一下 华影九天 的方案")
    assert "华影九天" in terms
    for junk in ("的这个", "是不是", "帮我", "我觉得", "我现在", "一个", "觉得", "看一下"):
        assert junk not in terms


def test_compose_hotwords_caps_auto_terms_and_skips_manual_variants(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(
        db_path=db_path,
        max_hotwords=10,
        min_accepts=1,
        max_terms=1000,
        max_auto_hotwords=2,
    )
    try:
        for _ in range(3):
            lexicon.observe_accepted("华影九天 沈宇 模型 项目")
        lexicon.observe_accepted("CodeX 很好用")
        merged = lexicon.compose_hotwords(["Codex", "Claude"])
        assert merged[:2] == ["Codex", "Claude"]
        # Learned "codex" (variant of manual "Codex") must not take a slot.
        assert "codex" not in merged
        # Auto quota is capped at 2 even though more terms qualify.
        assert len(merged) == 4
    finally:
        lexicon.close()


def test_prune_invalid_removes_legacy_fragment_terms(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(db_path=db_path, min_accepts=1, max_terms=1000)
    try:
        # Insert legacy-style junk directly, bypassing the new extraction filters.
        with lexicon._conn:
            lexicon._conn.executemany(
                "INSERT INTO lexicon_terms (term, seen_count, accept_count, last_seen, last_accept, blocked)"
                " VALUES (?, 1, 1, 0, 0, 0)",
                [("的这个",), ("是不是",), ("华影九天",), ("recordian",)],
            )
        removed = lexicon.prune_invalid()
        assert removed == 2
        remaining = {row[0] for row in lexicon._conn.execute("SELECT term FROM lexicon_terms").fetchall()}
        assert remaining == {"华影九天", "recordian"}
    finally:
        lexicon.close()

