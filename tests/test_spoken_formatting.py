from recordian.spoken_formatting import format_spoken_text


def test_cardinal_with_units() -> None:
    assert format_spoken_text("我有二十五个文件") == "我有25个文件"
    assert format_spoken_text("一百零二") == "102"
    assert format_spoken_text("一万二千") == "12000"
    assert format_spoken_text("两百分") == "200分"


def test_approximate_and_bare_readings_stay() -> None:
    assert format_spoken_text("一万二") == "一万二"
    assert format_spoken_text("第二十名") == "第二十名"
    assert format_spoken_text("三里屯见") == "三里屯见"
    assert format_spoken_text("我十分高兴") == "我十分高兴"


def test_digit_sequence_with_marker_keeps_leading_zeroes() -> None:
    assert format_spoken_text("编号零零一二") == "编号0012"
    assert format_spoken_text("密码是八八八八") == "密码是8888"
    assert format_spoken_text("房间号一二零四") == "房间号1204"


def test_standalone_positional_runs_convert() -> None:
    assert format_spoken_text("零零一二") == "0012"
    assert format_spoken_text("一二三") == "123"
    assert format_spoken_text("八八八八") == "8888"
    assert format_spoken_text("零零") == "00"


def test_decimal() -> None:
    assert format_spoken_text("三点一四") == "3.14"
    assert format_spoken_text("价格是二十五点五元") == "价格是25.5元"


def test_ipv4() -> None:
    assert format_spoken_text("一九二点一六八点五点一一一") == "192.168.5.111"
    # Digit-wise octets only; unit-style octets (十点…) are out of scope and stay.
    assert format_spoken_text("十点零点零点一") == "十点零点零点一"
    # Out-of-range octet is not an IP.
    assert format_spoken_text("九九九点一点一点一") == "九九九点一点一点一"


def test_date_keeps_chinese_separators() -> None:
    assert format_spoken_text("二〇二六年九月二十四日") == "2026年9月24日"
    assert format_spoken_text("二十六号下午三点半") == "26号下午三点半"
    # Impossible month stays untouched.
    assert format_spoken_text("二〇二六年十三月一日") == "二〇二六年十三月一日"


def test_time_words_stay() -> None:
    assert format_spoken_text("一点钟") == "一点钟"
    assert format_spoken_text("三点半") == "三点半"


def test_url_dot_strong_context_only() -> None:
    assert format_spoken_text("访问www点example点com") == "访问www.example.com"
    assert format_spoken_text("打开example点com") == "打开example.com"
    # Path case is preserved.
    assert format_spoken_text("www点Example点COM保持大小写") == "www.Example.COM保持大小写"
    # No TLD anchor: not a URL.
    assert format_spoken_text("邮箱是a点b") == "邮箱是a点b"


def test_ordinary_words_and_idioms_stay() -> None:
    for text in ("一点建议", "重点是这个", "一会儿再来", "一点点", "万无一失", "一心一意"):
        assert format_spoken_text(text) == text


def test_protected_spans_untouched() -> None:
    assert format_spoken_text("访问https://example点com/path") == "访问https://example.com/path"
    assert format_spoken_text("https://example点com/A点B") == "https://example.com/A点B"
    assert format_spoken_text("`https://example点com/A点B`") == "`https://example点com/A点B`"
    assert format_spoken_text("已经1.5了不要再改") == "已经1.5了不要再改"
    assert format_spoken_text("运行`echo 一二三`看看") == "运行`echo 一二三`看看"


def test_idempotent() -> None:
    samples = [
        "一九二点一六八点五点一一一",
        "三点一四",
        "访问www点example点com",
        "我有二十五个文件",
        "编号零零一二",
        "二〇二六年九月二十四日",
    ]
    for sample in samples:
        once = format_spoken_text(sample)
        assert format_spoken_text(once) == once


def test_combined_tail_and_current_text() -> None:
    combined = "上一段说到端口八零八零。当前句是一九二点一六八点五点一一一打不开"
    assert format_spoken_text(combined) == "上一段说到端口8080。当前句是192.168.5.111打不开"


def test_empty_and_blank() -> None:
    assert format_spoken_text("") == ""
    assert format_spoken_text("   ") == "   "


def test_review_mixed_and_spaced_ip_forms() -> None:
    assert format_spoken_text("192点168点五点111") == "192.168.5.111"
    assert format_spoken_text("一百九十二点一百六十八点五点一百一十一") == "192.168.5.111"
    assert format_spoken_text("一九二 点 一六八 点 五 点 一一一") == "192.168.5.111"


def test_review_spaced_url() -> None:
    assert format_spoken_text("www 点 example 点 com", ) == "www.example.com"


def test_review_zero_padded_cardinal() -> None:
    assert format_spoken_text("一万零二") == "10002"
    assert format_spoken_text("一万零二十") == "10020"
    assert format_spoken_text("一万二") == "一万二"


def test_review_negation_does_not_block_number_formatting() -> None:
    assert format_spoken_text("不是二十五而是三十五") == "不是25而是35"


def test_review_proper_nouns_and_calendar_words_stay() -> None:
    for text in ("十三香调味料", "三里屯见", "五一假期", "十一黄金周", "九一八事变", "万无一失", "一心一意"):
        assert format_spoken_text(text) == text
    # 十一 inside a longer numeral is not the calendar word.
    assert format_spoken_text("一百一十一") == "111"
    assert format_spoken_text("二十一") == "21"
    assert format_spoken_text("五一节") == "五一节"
    assert format_spoken_text("十一假期") == "十一假期"


def test_followup_bare_quantity_range_and_scheme_url() -> None:
    assert format_spoken_text("零") == "0"
    assert format_spoken_text("五") == "5"
    assert format_spoken_text("十") == "10"
    assert format_spoken_text("我有三个文件") == "我有3个文件"
    assert format_spoken_text("等待十秒") == "等待10秒"
    assert format_spoken_text("我两三天后来") == "我两三天后来"
    assert format_spoken_text("三四个") == "三四个"
    assert format_spoken_text("五六百") == "五六百"
    assert format_spoken_text("一会儿再来") == "一会儿再来"
    assert format_spoken_text("一点建议") == "一点建议"
    assert format_spoken_text("我十分高兴") == "我十分高兴"
    assert format_spoken_text("https://example点com/A点B") == "https://example.com/A点B"
    once = format_spoken_text("零")
    assert format_spoken_text(once) == "0"
    url = format_spoken_text("https://example点com/A点B")
    assert format_spoken_text(url) == url


def test_review_lexicalized_numerals_stay() -> None:
    for text in (
        "三三两两",
        "三五成群",
        "二八年华",
        "五四运动",
        "七七事变",
        "九九重阳",
        "九三学社",
        "三九严寒",
        "三番五次",
    ):
        assert format_spoken_text(text) == text
    # Same structural classes, not only the listed words.
    assert format_spoken_text("三三五五") == "三三五五"
    assert format_spoken_text("两次三番") == "两次三番"
    assert format_spoken_text("纪念五四。") == "纪念五四。"
    assert format_spoken_text("他说三五") == "他说三五"


def test_review_explicit_markers_override_lexical_protection() -> None:
    assert format_spoken_text("编号三五") == "编号35"
    assert format_spoken_text("密码三三两两") == "密码3322"
    assert format_spoken_text("房间号三三五五") == "房间号3355"
    assert format_spoken_text("编号零零一二") == "编号0012"


def test_review_unmarked_positional_codes_still_format() -> None:
    assert format_spoken_text("一二三") == "123"
    assert format_spoken_text("零零") == "00"
    assert format_spoken_text("八八八八") == "8888"
    assert format_spoken_text("零零一一") == "0011"
    assert format_spoken_text("端口八零八零") == "端口8080"


def test_review_units_and_measures_still_format() -> None:
    assert format_spoken_text("我有二十五个文件") == "我有25个文件"
    assert format_spoken_text("我有三个文件") == "我有3个文件"
    assert format_spoken_text("等待十秒") == "等待10秒"
    assert format_spoken_text("零") == "0"
    assert format_spoken_text("五十") == "50"


def test_review_idempotent_over_repro_set() -> None:
    for sample in (
        "三三两两", "三五成群", "二八年华", "五四运动", "七七事变",
        "九九重阳", "九三学社", "三九严寒", "三番五次", "三三五五",
        "编号三五", "密码三三两两", "房间号三三五五", "纪念五四。",
        "零零一一", "端口八零八零",
    ):
        once = format_spoken_text(sample)
        assert format_spoken_text(once) == once
