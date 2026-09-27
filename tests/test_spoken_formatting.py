from recordian.spoken_formatting import format_spoken_text


def test_cardinal_with_units() -> None:
    assert format_spoken_text("我有二十五个文件") == "我有25个文件"
    assert format_spoken_text("一百零二") == "102"
    assert format_spoken_text("一万二千") == "12000"
    assert format_spoken_text("两百分") == "200分"


def test_approximate_and_bare_readings_stay() -> None:
    assert format_spoken_text("一万二") == "一万二"
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
    assert format_spoken_text("二十六号下午三点半") == "26号下午3点半"
    # Impossible month stays untouched.
    assert format_spoken_text("二〇二六年十三月一日") == "二〇二六年十三月一日"


def test_clock_time_formats_and_suggestion_stays() -> None:
    assert format_spoken_text("一点钟") == "1点钟"
    assert format_spoken_text("三点半") == "3点半"
    assert format_spoken_text("一点建议") == "一点建议"


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


def test_long_press_run_and_short_run_boundary() -> None:
    # 长按位 digit-by-digit entry: a long unmarked run is positional.
    assert format_spoken_text("长按位一二三四五六七八九") == "长按位123456789"
    assert format_spoken_text("一二三四五") == "12345"
    assert format_spoken_text("零一二三") == "0123"
    # A bare two-character pair stays Chinese: approximate pair or lexical run.
    assert format_spoken_text("三四") == "三四"
    assert format_spoken_text("三五成群") == "三五成群"
    assert format_spoken_text("他说三五") == "他说三五"
    # Three or more characters are positional even when they contain 三四.
    assert format_spoken_text("一二三三四") == "12334"


def test_yao_is_digit_one_in_runs_but_not_in_words() -> None:
    assert format_spoken_text("幺一幺服务器") == "111服务器"
    assert format_spoken_text("服务器幺幺幺") == "服务器111"
    assert format_spoken_text("幺幺") == "11"
    assert format_spoken_text("幺幺零") == "110"
    assert format_spoken_text("端口幺二三四") == "端口1234"
    assert format_spoken_text("电话幺三八零零幺三八零零零") == "电话13800138000"
    assert format_spoken_text("幺九二点幺六八点幺点幺") == "192.168.1.1"
    for text in ("幺妹", "幺蛾子", "幺儿", "幺"):
        assert format_spoken_text(text) == text


def test_percent_forms() -> None:
    assert format_spoken_text("百分之三十五") == "35%"
    assert format_spoken_text("百分之三点五") == "3.5%"
    assert format_spoken_text("百分之35") == "35%"
    assert format_spoken_text("增长百分之一百二十") == "增长120%"


def test_explicit_ordinal_forms_and_lexical_first() -> None:
    assert format_spoken_text("第十二个") == "第12个"
    assert format_spoken_text("第十二章") == "第12章"
    assert format_spoken_text("第二十名") == "第20名"
    # A single digit after 第 is a lexical form, not an ordinal value.
    for text in ("第三方", "第一名"):
        assert format_spoken_text(text) == text


def test_clock_forms_and_point_words_stay() -> None:
    assert format_spoken_text("下午三点半") == "下午3点半"
    assert format_spoken_text("一点钟") == "1点钟"
    for text in ("一点建议", "重点是这个"):
        assert format_spoken_text(text) == text


def test_bare_eleven_and_marker_pair() -> None:
    assert format_spoken_text("十一") == "11"
    assert format_spoken_text("数字三四") == "数字34"
    assert format_spoken_text("号码幺幺零") == "号码110"
    for text in ("十一黄金周", "五一假期", "五一节", "三番五次", "代码", "一万二", "两三天", "三四个"):
        assert format_spoken_text(text) == text


def test_existing_ascii_and_formed_urls_untouched() -> None:
    for text in (
        "已经有13800138000了不要再改",
        "端口8080",
        "http://192.168.1.1/幺妹",
        "运行`echo 一二三`看看",
        "已经1.5了不要再改",
    ):
        assert format_spoken_text(text) == text


def test_new_forms_are_idempotent() -> None:
    for sample in (
        "长按位一二三四五六七八九", "一二三三四", "幺一幺服务器", "幺幺", "幺幺零",
        "服务器幺幺幺", "端口幺二三四", "电话幺三八零零幺三八零零零",
        "幺九二点幺六八点幺点幺", "百分之三点五", "百分之35", "第十二个",
        "第二十名", "下午三点半", "一点钟", "十一", "数字三四", "号码幺幺零",
    ):
        once = format_spoken_text(sample)
        assert format_spoken_text(once) == once


def test_marker_captures_whole_cardinal_before_its_first_digit() -> None:
    """An explicit marker claims the complete numeral, not just its first digit."""
    assert format_spoken_text("端口一百零二") == "端口102"
    assert format_spoken_text("编号一百二十三") == "编号123"
    assert format_spoken_text("数字三百二十") == "数字320"
    # A suffix marker must not leave a truncated digit behind either.
    assert format_spoken_text("一百一服务器") == "101服务器"
    assert format_spoken_text("端口幺二三四") == "端口1234"
    assert format_spoken_text("编号零零一二") == "编号0012"


def test_determiner_and_locative_yi_is_not_a_quantity() -> None:
    """定位/指代前缀 + 一 + 量词 is grammatical, not the number 1."""
    for text in ("下一个", "上一个", "另一个", "每一个", "这一个", "那一个", "哪一个", "前一个"):
        assert format_spoken_text(text) == text, text
    assert format_spoken_text("下一个是端口幺二") == "下一个是端口12"
    # An explicit quantity after a verb still converts.
    assert format_spoken_text("我有一个文件") == "我有1个文件"
    assert format_spoken_text("多一个文件") == "多1个文件"


def test_clock_hour_minute_and_month_day_forms() -> None:
    assert format_spoken_text("下午三点十五分") == "下午3点15分"
    assert format_spoken_text("上午九点零五分") == "上午9点05分"
    assert format_spoken_text("九月二十四号") == "9月24号"
    assert format_spoken_text("九月二十四日") == "9月24日"
    assert format_spoken_text("十二月三十一号") == "12月31号"
    # Ambiguous or lexical neighbours stay: 一点建议, 三点一四 is a decimal,
    # 三点半 is already a clock form, and the full year date keeps working.
    assert format_spoken_text("一点建议") == "一点建议"
    assert format_spoken_text("三点一四") == "3.14"
    assert format_spoken_text("下午三点半") == "下午3点半"
    assert format_spoken_text("二〇二六年九月二十四日") == "2026年9月24日"
    assert format_spoken_text("编号零零一二") == "编号0012"
