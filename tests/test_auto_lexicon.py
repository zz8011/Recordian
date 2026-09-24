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


def test_legacy_observe_is_not_recorded_as_user_confirmed(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(db_path=db_path, min_accepts=1, max_terms=1000)
    try:
        lexicon.observe_accepted("Recordian 项目")
        info = lexicon.term_info("recordian")
        assert info is not None
        assert info["accept_count"] >= 1
        assert info["confirm_count"] == 0
        assert info["last_source"] == "legacy"
        assert info["sources"].get("user_confirmed", 0) == 0
        assert "recordian" in lexicon.compose_hotwords([])
    finally:
        lexicon.close()


def test_pipeline_sources_do_not_confirm_terms(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(db_path=db_path, min_accepts=1, max_terms=1000)
    try:
        lexicon.observe_accepted("Recordian 项目", source="asr")
        lexicon.observe_accepted("Recordian 发布", source="refined")
        lexicon.observe_accepted("Recordian 说明", source="corrected")
        assert lexicon.compose_hotwords([]) == []
        info = lexicon.term_info("recordian")
        assert info is not None
        assert info["accept_count"] == 0
        assert info["confirm_count"] == 0
        assert info["sources"].get("user_confirmed", 0) == 0
        assert info["sources"]["asr"] >= 1
        assert info["sources"]["refined"] >= 1
        assert info["sources"]["corrected"] >= 1
        lexicon.observe_accepted("Recordian 定稿", source="user_confirmed")
        confirmed = lexicon.term_info("recordian")
        assert confirmed is not None
        assert confirmed["confirm_count"] == 1
        assert "recordian" in lexicon.compose_hotwords([])
    finally:
        lexicon.close()


def test_old_lexicon_schema_stays_readable(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE lexicon_terms (
            term TEXT PRIMARY KEY,
            seen_count INTEGER NOT NULL DEFAULT 0,
            accept_count INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0,
            last_accept INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO lexicon_terms (term, seen_count, accept_count, last_seen, last_accept, blocked)"
        " VALUES ('recordian', 3, 3, 1, 1, 0)"
    )
    conn.commit()
    conn.close()

    lexicon = AutoLexicon(db_path=db_path, min_accepts=2, max_terms=1000)
    try:
        assert "recordian" in lexicon.compose_hotwords([])
        info = lexicon.term_info("recordian")
        assert info is not None
        assert info["accept_count"] == 3
        assert info["confirm_count"] == 0
        assert info["last_source"] == ""
        lexicon.observe_accepted("OpenClaw 工具", source="asr")
        assert lexicon.term_info("recordian")["accept_count"] == 3
        assert "openclaw" not in lexicon.compose_hotwords([])
        assert "recordian" in lexicon.compose_hotwords([])
    finally:
        lexicon.close()


def test_prune_limit_removes_source_orphans_and_keeps_top_terms(tmp_path: Path) -> None:
    db_path = tmp_path / "auto_lexicon.db"
    lexicon = AutoLexicon(db_path=db_path, max_terms=2, min_accepts=1)
    try:
        # max_terms is floored at 100. Insert 101 terms so exactly the lowest is removed.
        term_rows = [("keep-a", 10, 1000), ("keep-b", 9, 999)]
        term_rows.extend((f"m{index:03d}", 1, index + 1) for index in range(98))
        term_rows.append(("gamma", 1, 0))
        with lexicon._conn:
            lexicon._conn.executemany(
                """
                INSERT INTO lexicon_terms (
                    term, seen_count, accept_count, last_seen, last_accept, blocked,
                    last_source, confirm_count
                ) VALUES (?, 1, ?, 1, ?, 0, 'user_confirmed', 1)
                """,
                term_rows,
            )
            lexicon._conn.executemany(
                "INSERT INTO lexicon_term_sources (term, source, count, last_seen) VALUES (?, ?, 1, 1)",
                [("keep-a", "user_confirmed"), ("keep-b", "user_confirmed"), ("gamma", "asr"), ("orphan", "asr")],
            )
        lexicon._prune_to_limit_locked()
        terms = {row[0] for row in lexicon._conn.execute("SELECT term FROM lexicon_terms")}
        sources = {row[0] for row in lexicon._conn.execute("SELECT DISTINCT term FROM lexicon_term_sources")}
        assert len(terms) == 100
        assert "keep-a" in terms and "keep-b" in terms
        assert "gamma" not in terms
        assert "orphan" not in sources and "gamma" not in sources
        assert sources <= terms
        assert lexicon.term_info("keep-a")["sources"]["user_confirmed"] == 1
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

