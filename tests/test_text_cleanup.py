from recordian.text_cleanup import _normalize_final_text, wrap_overlay_caption


def test_wrap_overlay_caption_keeps_short_text() -> None:
    assert wrap_overlay_caption("你好") == "你好"
    assert wrap_overlay_caption("  ") == ""


def test_wrap_overlay_caption_wraps_and_keeps_tail() -> None:
    caption = wrap_overlay_caption("一二三四五六七八九十" * 8, max_chars=10, max_lines=3)
    assert caption.startswith("…")
    assert caption.count("\n") == 2
    assert caption.endswith("九十")


def test_dedupe_preserves_literal_numeric_runs() -> None:
    # Dedupe itself never collapses numeric runs; the formatter then applies.
    from recordian.text_cleanup import _dedupe_repeats

    assert _dedupe_repeats("八八八八") == "八八八八"
    assert _dedupe_repeats("1111") == "1111"
    assert _dedupe_repeats("零零") == "零零"
    # Spoken positional digits survive dedupe and format once, idempotently.
    assert _normalize_final_text("八八八八") == "8888"
    assert _normalize_final_text("零零") == "00"
    assert _normalize_final_text("1111") == "1111"


def test_dedupe_preserves_url_email_code_literals() -> None:
    from recordian.text_cleanup import _dedupe_repeats

    assert _dedupe_repeats("密码是12341234") == "密码是12341234"
    assert _dedupe_repeats("访问example.com11") == "访问example.com11"
    assert _dedupe_repeats("打开example.com打开example.com") == "打开example.com"


def test_dedupe_still_collapses_non_numeric_repeats() -> None:
    assert _normalize_final_text("哈哈哈哈") == "哈"
    assert _normalize_final_text("hello worldhello world") == "hello world"


def test_final_text_formats_spoken_numbers_without_config() -> None:
    assert _normalize_final_text("编号零零一二") == "编号0012"
    assert _normalize_final_text("我有二十五个文件") == "我有25个文件"
    assert _normalize_final_text("一点建议") == "一点建议"


def test_normalize_final_text_idempotent() -> None:
    once = _normalize_final_text("编号零零一二，一九二点一六八点五点一一一")
    assert once == "编号0012，192.168.5.111"
    assert _normalize_final_text(once) == once
    assert _normalize_final_text("零") == "0"
    assert _normalize_final_text(_normalize_final_text("零")) == "0"
    assert _normalize_final_text("1111") == "1111"


def test_normalize_final_text_followup_cases() -> None:
    assert _normalize_final_text("五") == "5"
    assert _normalize_final_text("十") == "10"
    assert _normalize_final_text("我有三个文件") == "我有3个文件"
    assert _normalize_final_text("等待十秒") == "等待10秒"
    assert _normalize_final_text("我两三天后来") == "我两三天后来"
    assert _normalize_final_text("三四个") == "三四个"
    assert _normalize_final_text("五六百") == "五六百"
    assert _normalize_final_text("二十一") == "21"
    assert _normalize_final_text("一百一十一") == "111"
    assert _normalize_final_text("十一假期") == "十一假期"
    assert _normalize_final_text("五一节") == "五一节"
    assert _normalize_final_text("https://example点com/A点B") == "https://example.com/A点B"
    assert _normalize_final_text("`https://example点com/A点B`") == "`https://example点com/A点B`"
    assert _normalize_final_text("一点建议") == "一点建议"
    assert _normalize_final_text("三里屯见") == "三里屯见"
    assert _normalize_final_text("十三香") == "十三香"


def test_final_text_keeps_lexicalized_numerals() -> None:
    for text in (
        "三三两两", "三五成群", "二八年华", "五四运动", "七七事变",
        "九九重阳", "九三学社", "三九严寒", "三番五次",
    ):
        assert _normalize_final_text(text) == text


def test_final_text_lexicalized_numerals_idempotent() -> None:
    for sample in ("三三两两", "三番五次", "编号三五", "密码三三两两", "五四运动"):
        once = _normalize_final_text(sample)
        assert _normalize_final_text(once) == once


def test_final_text_markers_runs_and_measures_unaffected() -> None:
    assert _normalize_final_text("编号三五") == "编号35"
    assert _normalize_final_text("密码三三两两") == "密码3322"
    assert _normalize_final_text("一二三") == "123"
    assert _normalize_final_text("零零") == "00"
    assert _normalize_final_text("八八八八") == "8888"
    assert _normalize_final_text("零零一一") == "0011"
    assert _normalize_final_text("我有二十五个文件") == "我有25个文件"
    assert _normalize_final_text("我有三个文件") == "我有3个文件"
    assert _normalize_final_text("等待十秒") == "等待10秒"
    assert _normalize_final_text("五十") == "50"
