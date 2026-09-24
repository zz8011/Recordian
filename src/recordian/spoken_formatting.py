"""Deterministic spoken-number and URL-dot formatting for ASR finals.

Pure and idempotent. Converts only structurally unambiguous spoken forms:

- cardinal with units: 我有二十五个文件 → 我有25个文件, 一百零二 → 102,
  一万零二 → 10002 (一万二 / 两三天 / 三四个 / 五六百 stay approximate)
- a single digit or 十 on its own, or before an explicit measure word:
  零 → 0, 五 → 5, 十 → 10, 三个 → 3个, 十秒 → 10秒
- standalone positional runs: 一二三 → 123, 零零 → 00 (leading zeros kept)
- digit sequence after an explicit marker: 编号零零一二 → 编号0012
- decimal: 三点一四 → 3.14
- IPv4 (digit-wise, cardinal, or latin octets, optional spaces):
  一九二点一六八点五点一一一 / 192点168点五点111 /
  一百九十二点一百六十八点五点一百一十一 → 192.168.5.111
- date: 二〇二六年九月二十四日 → 2026年9月24日 (separators retained)
- URL dots in strong URL structure (optional spaces):
  www点example点com / www 点 example 点 com → www.example.com
- scheme already recognized, host still spoken:
  https://example点com/A点B → https://example.com/A点B
  (only the host dots; the path stays literal)

Ordinary words and proper nouns (一点建议 / 重点 / 一会儿 / 一点点 /
万无一失 / 一心一意 / 三里屯 / 十三香 / 五一假期 / 十一黄金周 / 十一假期 /
第三方 / 幺妹 / 幺蛾子) stay unchanged, as do lexicalized numeric
expressions: an unmarked numeral run glued to a following CJK character
is read as part of that word (三五成群 / 二八年华 / 五四运动 / 七七事变 /
九九重阳 / 九三学社 / 三九严寒), a two-digit unmarked run also needs a
non-CJK left boundary (纪念五四), AABB reduplication is lexical
(三三两两 / 三三五五, but not a run containing 零: 零零一一 → 0011), and
the 番/次 idiom frames are fixed (三番五次 / 两次三番). A bare adjacent
pair is approximate (三四 / 两三天 / 三四个 / 五六百); a longer
digit-by-digit run still formats when it merely contains such a pair
(一二三四五六七八九 → 123456789). 幺 is the digit 1 inside positional
runs, marker spans, and IP octets (幺幺 → 11, 幺幺幺 → 111), not inside
ordinary words. Explicit markers claim their digits first, in either direction
(编号三五 → 编号35, 幺幺幺服务器 → 111服务器). Clear ordinals with a unit
format (第十二个 → 第12个); 第 plus a single digit does not (第三方).
Clock forms format (三点半 → 3点半, 一点钟 → 1点钟). Percents format
(百分之三十五 → 35%, 百分之三点五 → 3.5%, 百分之35 → 35%). A bare 十一
is eleven; only the full holiday phrases stay Chinese. URLs, emails,
code spans, and existing digit runs are protected; negation does NOT
block number formatting (不是二十五而是三十五 → 不是25而是35).
"""

from __future__ import annotations

import re

from .hotword_corrector import (
    _BACKTICK_RE,
    _CODE_RE,
    _EMAIL_RE,
    _FENCE_RE,
    _LATIN_NUM_RE,
    _URL_RE,
    _overlaps,
)


def _literal_spans(text: str) -> list[tuple[int, int]]:
    """Code, fences, and emails. Scheme URLs are handled separately so a
    spoken host dot is not frozen inside the whole URL."""
    spans: list[tuple[int, int]] = []
    for pattern in (_FENCE_RE, _BACKTICK_RE, _EMAIL_RE, _CODE_RE):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return spans


def _formatting_protected_spans(text: str) -> list[tuple[int, int]]:
    """URLs, emails, and code spans are never reformatted.

    Existing ASCII digit runs are handled separately (they may be octets
    inside a spoken IP). Unlike hotword replacement, CJK number runs are
    *not* protected here — converting them is this module's job.
    """
    spans: list[tuple[int, int]] = []
    for pattern in (_FENCE_RE, _BACKTICK_RE, _URL_RE, _EMAIL_RE, _CODE_RE):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return spans

_DIGIT_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "幺": 1,
}
_UNITS = {"十": 10, "百": 100, "千": 1000}
# 幺 is a spoken digit (号码/端口/IP), not a cardinal word. Keep it out of
# _DIGIT / _NUMRUN so 幺妹 and 一点-style words are not parsed as quantities.
_DIGIT = "零〇一二两三四五六七八九"
_POS_DIGIT = _DIGIT + "幺"
_NUMRUN = _DIGIT + "十百千万亿"

_DIGIT_RE = re.compile(rf"[{_DIGIT}]+")
_POS_DIGIT_RE = re.compile(rf"[{_POS_DIGIT}]+")
_OCTET = rf"(?:[0-9]{{1,3}}|[{_POS_DIGIT}]{{1,3}}|[{_DIGIT}十百]{{2,6}})"
_IP_RE = re.compile(
    rf"(?<![{_NUMRUN}A-Za-z0-9.])({_OCTET})\s*点\s*({_OCTET})\s*点\s*({_OCTET})\s*点\s*({_OCTET})(?![{_NUMRUN}A-Za-z0-9])"
)
_DATE_RE = re.compile(
    rf"(?<![{_NUMRUN}])([{_DIGIT}]{{4}})年([{_NUMRUN}]{{1,3}})月([{_NUMRUN}]{{1,3}})(日|号)(?![{_NUMRUN}])"
)
_TLDS = ("com", "cn", "net", "org", "io", "dev", "ai", "app", "me", "info",
         "biz", "edu", "gov", "xyz", "top", "cc", "tv", "co")
_URL_DOT_RE = re.compile(
    r"(?<![A-Za-z0-9.])(www|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"((?:\s*点\s*[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
)
_DECIMAL_RE = re.compile(rf"(?<![{_NUMRUN}.点])([{_NUMRUN}]{{1,9}})点([{_DIGIT}]{{1,6}})(?![{_NUMRUN}.点])")
# Explicit number markers. Shared as the single source of truth with
# realtime_asr's raw-tail seam, which used to carry its own shorter list and
# therefore missed 手机号/电话/数字.
NUMBER_MARKERS = (
    "房间号", "座位号", "手机号", "电话号码", "序列号", "车牌号", "航班号",
    "编号", "号码", "房号", "门牌", "分机", "工号", "学号", "车牌",
    "电话", "端口", "服务器", "数字", "密码", "QQ", "qq", "IP", "ip",
)
_MARKER_ALT = "|".join(sorted(NUMBER_MARKERS, key=len, reverse=True))
# Every character a spoken number can be built from, 幺 included. A marker
# claims the whole run: matching only its first digit stranded the rest
# (端口一百零二 → 端口1百零二).
_NUMERAL_CHARS = _DIGIT + "十百千万亿幺"
_SEQ_RE = re.compile(
    rf"({_MARKER_ALT})(?:是|为|[:：])?\s*([{_NUMERAL_CHARS}]{{1,}})"
    rf"(?![{_NUMERAL_CHARS}.点．])"
)
# Digits spoken immediately before the marker: 幺幺幺服务器 → 111服务器,
# 一百一服务器 → 101服务器. The left boundary keeps the run complete: a
# suffix match may not start inside a longer numeral.
_SUFFIX_MARKERS = "服务器|端口|号码|编号|电话|数字"
_SUFFIX_SEQ_RE = re.compile(
    rf"(?<![{_NUMERAL_CHARS}.点．])([{_NUMERAL_CHARS}]{{1,}})"
    rf"({_SUFFIX_MARKERS})(?![{_NUMERAL_CHARS}])"
)
_PERCENT_RE = re.compile(
    rf"百分之\s*(?:([0-9]+(?:\.[0-9]+)?)|([{_NUMRUN}]{{1,9}})点([{_DIGIT}]{{1,6}})|([{_NUMRUN}]{{1,9}}))"
)
# 第十二个 / 第十二章. A single digit (第三方) is not an explicit ordinal value.
_ORDINAL_RE = re.compile(rf"第([{_NUMRUN}]{{2,9}})(?![{_NUMRUN}])")
# 三点半 / 一点钟. 一点建议 has neither 半 nor 钟.
_TIME_RE = re.compile(rf"(?<![{_NUMRUN}])([{_NUMRUN}]{{1,6}})点(半|钟)")
# HH点MM分. A one-character minute needs an explicit daypart (下午三点五分);
# without it 三点五分 stays the decimal handled by _DECIMAL_RE.
_DAYPART = "上午|下午|早上|晚上|中午|凌晨|傍晚|半夜|夜里|清晨"
_TIME_HM_RE = re.compile(
    rf"(?<![{_NUMRUN}])({_DAYPART})?([{_NUMRUN}]{{1,6}})点"
    rf"([{_DIGIT}十]{{1,3}})分(?![{_NUMRUN}钟])"
)
# 九月二十四号 / 九月二十四日 without a year. The year form runs first and
# the 年 lookbehind keeps a full date from being half-claimed.
_MONTH_DAY_RE = re.compile(
    rf"(?<![{_NUMRUN}年])([{_NUMRUN}]{{1,3}})月([{_NUMRUN}]{{1,3}})(日|号)"
    rf"(?![{_NUMRUN}])"
)
_CARDINAL_RE = re.compile(rf"(?<![{_NUMRUN}第.点])([{_NUMRUN}]{{2,9}})(?![{_NUMRUN}点])")
# An unmarked positional run must be token-final (not glued to a following
# CJK character: 三五成群, 五四运动); a two-digit run also needs a non-CJK
# boundary on the left (纪念五四 / 他说三五 are not codes).
_POSITIONAL_RE = re.compile(
    rf"(?<![{_NUMRUN}A-Za-z0-9.点幺])([{_POS_DIGIT}]{{2,}})(?![{_NUMRUN}A-Za-z0-9.点\u4e00-\u9fff])"
)
# Bare neighboring pair (三四), not a longer digit-by-digit run that contains one.
_APPROX_PAIRS = frozenset({"一两", "两三", "三四", "四五", "五六", "六七", "七八", "八九"})

# Proper nouns and complete calendar phrases. Bare 十一 is a value (11);
# only the full holiday names stay Chinese.
_PROTECTED_WORDS = (
    "十三香", "九一八", "一二九", "五一", "六一", "七一", "八一", "三八",
    "十一黄金周", "十一假期",
    "三番五次", "两次三番",
)


def _reduplicated_digits(token: str) -> bool:
    """AABB reduplication (三三两两, 三三五五) is lexical, not a code.

    AA and AAAA runs (零零, 八八八八) are literal digit sequences and still
    format; a run containing 零 is read as a positional code (零零一一).
    """
    if len(token) < 4 or len(token) % 2 or "零" in token or "〇" in token:
        return False
    blocks = [token[index : index + 2] for index in range(0, len(token), 2)]
    doubled = all(block[0] == block[1] for block in blocks)
    return doubled and len({block[0] for block in blocks}) >= 2


def _glued_to_cjk(text: str, index: int) -> bool:
    """True when the character before *index* is a CJK ideograph."""
    return index > 0 and "\u4e00" <= text[index - 1] <= "\u9fff"


def _digits_to_ascii(token: str) -> str:
    return "".join(str(_DIGIT_MAP[ch]) for ch in token)


def _approximate_digits(token: str) -> bool:
    """True only when the whole token is one approximate pair.

    一二三四五六七八九 contains 三四/五六 but is a digit-by-digit code.
    两三天 / 三四个 / 五六百 never reach this check: a following CJK
    character or unit keeps them out of the positional run.
    """
    return len(token) == 2 and token in _APPROX_PAIRS


def _clock_value(token: str) -> str | None:
    if len(token) == 1 or token == "十":
        return _single_number(token)
    value = _parse_cardinal(token)
    if value is None:
        return None
    return str(value)


def _clock_field(token: str) -> str | None:
    """One HH or MM field. A multi-digit positional run (零五) is read
    digit-wise; 零五 alone is not a cardinal section."""
    if len(token) > 1 and _DIGIT_RE.fullmatch(token):
        return str(int(_digits_to_ascii(token)))
    return _clock_value(token)


def _percent_text(match: re.Match[str]) -> str | None:
    arabic = match.group(1)
    if arabic:
        return f"{arabic}%"
    if match.group(2) is not None:
        integer = match.group(2)
        if _DIGIT_RE.fullmatch(integer):
            whole = int(_digits_to_ascii(integer)) if len(integer) <= 2 else None
        else:
            whole = _parse_cardinal(integer)
        if whole is None:
            return None
        return f"{whole}.{_digits_to_ascii(match.group(3))}%"
    token = match.group(4)
    if token is None:
        return None
    if _DIGIT_RE.fullmatch(token):
        if len(token) != 1:
            return None
        return f"{_DIGIT_MAP[token]}%"
    value = _parse_cardinal(token)
    if value is None:
        return None
    return f"{value}%"


def _parse_section(text: str, *, allow_bare: bool) -> int | None:
    """Parse one 万/亿-free section strictly. Positional digit runs rejected."""
    if not text:
        return 0 if allow_bare else None
    total = 0
    pending: int | None = None
    zero_pending = False
    seen_unit = False
    for ch in text:
        digit = _DIGIT_MAP.get(ch)
        if digit is not None:
            if digit == 0:
                if pending is not None:
                    return None
                pending = 0
                zero_pending = True
                continue
            if pending is not None and not zero_pending:
                return None
            pending = digit
            zero_pending = False
            continue
        unit = _UNITS.get(ch)
        if unit is None:
            return None
        seen_unit = True
        base = pending if pending is not None else (1 if ch == "十" else None)
        if not base:
            return None
        total += base * unit
        pending = None
        zero_pending = False
    if pending is not None:
        if pending == 0 and not seen_unit:
            return None
        total += pending
    if not seen_unit and not allow_bare:
        return None
    if not seen_unit and pending is None:
        return None
    return total


def _parse_cardinal(text: str) -> int | None:
    """Strict Chinese cardinal. 一万二-style approximate readings rejected.

    A single leading 零 in the section after 万/亿 is zero padding
    (一万零二 → 10002); bare 零二 without a marker stays positional.
    """
    total = 0
    rest = text
    marker_seen = False
    for marker, multiplier in (("亿", 100_000_000), ("万", 10_000)):
        if marker in rest:
            marker_seen = True
            head, _, rest = rest.partition(marker)
            section = _parse_section(head, allow_bare=True)
            if not section:
                return None
            total += section * multiplier
    if rest:
        zero_padded = marker_seen and len(rest) >= 2 and rest[0] in "零〇"
        if zero_padded:
            rest = rest[1:]
        section = _parse_section(rest, allow_bare=zero_padded)
        if section is None:
            return None
        total += section
    return total if total > 0 else None


def _parse_octet(text: str) -> int | None:
    """IPv4 octet: latin digits, positional CJK digits (including 幺), or a strict cardinal."""
    if text.isascii():
        return int(text) if text.isdigit() else None
    if _POS_DIGIT_RE.fullmatch(text) and ("幺" in text or _DIGIT_RE.fullmatch(text)):
        return int(_digits_to_ascii(text))
    return _parse_cardinal(text)


def _parse_small(text: str) -> int | None:
    """Month/day field: cardinal with units, or a short positional run."""
    if _DIGIT_RE.fullmatch(text):
        return int(_digits_to_ascii(text))
    return _parse_cardinal(text)


def _marker_number_text(token: str) -> str | None:
    """Digits after an explicit marker.

    A positional run is rendered verbatim (leading zeros kept: 编号零零一二 →
    0012); anything else must be a strict cardinal (端口一百零二 → 102).
    """
    if _POS_DIGIT_RE.fullmatch(token):
        return _digits_to_ascii(token)
    value = _parse_cardinal(token)
    return None if value is None else str(value)


# Explicit measure words. 分/天/下/点 are omitted: 十分, 一两天, 一下, 一点
# are ordinary words, and 两三天 / 三四个 must stay approximate.
_MEASURES = (
    "分钟", "小时", "公斤", "个", "只", "条", "本", "张", "次", "遍",
    "件", "份", "套", "台", "辆", "页", "米", "秒", "元", "倍",
)
_MEASURE_ALT = "|".join(sorted(_MEASURES, key=len, reverse=True))
_MEASURE_RE = re.compile(
    rf"(?<![{_NUMRUN}第])([{_DIGIT}十])(?![{_NUMRUN}])({_MEASURE_ALT})"
)
# 定位/指代前缀: 下一个, 另一个, 每一个, 这一个, 同一个 … here 一+量词 is a
# grammatical determiner, not the quantity 1 (我有一个文件 still converts).
_DETERMINER_PREFIXES = frozenset("这那哪每另上下前后头某本此该唯同么")
_STANDALONE_RE = re.compile(
    rf"(?<![{_NUMRUN}A-Za-z0-9\u4e00-\u9fff])([{_DIGIT}]|十)"
    rf"(?![{_NUMRUN}A-Za-z0-9\u4e00-\u9fff])"
)
_SCHEME_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")


def _single_number(token: str) -> str | None:
    if token == "十":
        return "10"
    digit = _DIGIT_MAP.get(token)
    if digit is None:
        return None
    return str(digit)


def _rewrite_scheme_host(url: str) -> str:
    """Turn spoken dots in the host into '.'; leave the path untouched."""
    matched = re.match(r"(https?://)(.*)", url, re.IGNORECASE)
    if matched is None:
        return url
    prefix, rest = matched.group(1), matched.group(2)
    cut = len(rest)
    for sep in "/?#":
        index = rest.find(sep)
        if index != -1:
            cut = min(cut, index)
    host, tail = rest[:cut], rest[cut:]
    if "点" not in host:
        return url
    labels = [part for part in re.split(r"\s*点\s*", host) if part]
    if len(labels) < 2 or labels[-1].lower() not in _TLDS:
        return url
    if any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        return url
    return prefix + ".".join(labels) + tail


def _format_url_dot(match: re.Match[str]) -> str:
    head = match.group(1)
    parts = [part for part in re.split(r"\s*点\s*", match.group(2)) if part]
    if not parts or parts[-1].lower() not in _TLDS:
        return match.group(0)
    return ".".join([head, *parts])


def _protected_word_spans(text: str) -> list[tuple[int, int]]:
    """Blocked proper nouns, only when not embedded in a longer numeral
    (一百一十一 contains 十一 but is not 十一 the calendar word)."""
    spans: list[tuple[int, int]] = []
    for word in _PROTECTED_WORDS:
        start = text.find(word)
        while start >= 0:
            end = start + len(word)
            before_ok = start == 0 or text[start - 1] not in _NUMRUN
            after_ok = end == len(text) or text[end] not in _NUMRUN
            if before_ok and after_ok:
                spans.append((start, end))
            start = text.find(word, start + 1)
    return spans


def format_spoken_text(text: str) -> str:
    """Return *text* with unambiguous spoken numbers/URL dots formatted.

    Idempotent: emitted ASCII digits and URLs are protected spans on a
    second pass, so ``format_spoken_text(format_spoken_text(x))`` equals
    ``format_spoken_text(x)``.
    """
    source = str(text)
    if not source.strip():
        return source
    literal = _literal_spans(source)
    word = _protected_word_spans(source)
    latin = [(m.start(), m.end()) for m in _LATIN_NUM_RE.finditer(source)]
    edits: list[tuple[int, int, str]] = []
    claimed: list[tuple[int, int]] = []
    scheme_spans: list[tuple[int, int]] = []
    for match in _SCHEME_URL_RE.finditer(source):
        if _overlaps(match.start(), match.end(), literal):
            continue
        rewritten = _rewrite_scheme_host(match.group(0))
        if rewritten == match.group(0):
            continue
        scheme_spans.append((match.start(), match.end()))
        claimed.append((match.start(), match.end()))
        edits.append((match.start(), match.end(), rewritten))
    url_spans = [
        (match.start(), match.end())
        for match in _URL_RE.finditer(source)
        if not _overlaps(match.start(), match.end(), scheme_spans)
    ]
    hard = literal + word + url_spans
    protected = hard + latin

    def claim(start: int, end: int, replacement: str, *, allow_latin: bool = False) -> None:
        wall = hard if allow_latin else protected
        if _overlaps(start, end, wall) or _overlaps(start, end, claimed):
            return
        claimed.append((start, end))
        edits.append((start, end, replacement))

    for match in _IP_RE.finditer(source):
        # Latin octets inside the match are fine; only hard protection blocks.
        if _overlaps(match.start(), match.end(), hard) or _overlaps(match.start(), match.end(), claimed):
            continue
        groups = [_parse_octet(g) for g in match.groups()]
        if all(g is not None and 0 <= g <= 255 for g in groups):
            claimed.append((match.start(), match.end()))
            edits.append((match.start(), match.end(), ".".join(str(g) for g in groups)))

    for match in _DATE_RE.finditer(source):
        year = int(_digits_to_ascii(match.group(1)))
        month = _parse_small(match.group(2))
        day = _parse_small(match.group(3))
        if month is None or day is None or not 1 <= month <= 12 or not 1 <= day <= 31:
            # Invalid date (e.g. 十三月): occupy the span so the cardinal
            # pass does not rewrite part of it.
            claimed.append((match.start(), match.end()))
            continue
        claim(match.start(), match.end(), f"{year}年{month}月{day}{match.group(4)}")

    for match in _MONTH_DAY_RE.finditer(source):
        month = _parse_small(match.group(1))
        day = _parse_small(match.group(2))
        if month is None or day is None or not 1 <= month <= 12 or not 1 <= day <= 31:
            # Invalid month/day (e.g. 十三月): occupy the span so no pass
            # rewrites a fragment of it.
            claimed.append((match.start(), match.end()))
            continue
        claim(match.start(), match.end(), f"{month}月{day}{match.group(3)}")

    for match in _URL_DOT_RE.finditer(source):
        replaced = _format_url_dot(match)
        if replaced != match.group(0):
            claim(match.start(), match.end(), replaced)

    for match in _PERCENT_RE.finditer(source):
        rendered = _percent_text(match)
        if rendered is not None:
            # 百分之35: the latin digits are the number being rewritten.
            claim(match.start(), match.end(), rendered, allow_latin=True)

    for match in _TIME_RE.finditer(source):
        rendered = _clock_value(match.group(1))
        if rendered is not None:
            claim(match.start(1), match.end(1), rendered)

    for match in _TIME_HM_RE.finditer(source):
        minute_token = match.group(3)
        if len(minute_token) < 2 and match.group(1) is None:
            # 三点五分 with no daypart is the decimal 3.5, not a clock time.
            continue
        hour = _clock_field(match.group(2))
        minute = _clock_field(minute_token)
        if hour is None or minute is None:
            continue
        if not (0 <= int(hour) <= 24 and 0 <= int(minute) <= 59):
            continue
        if len(minute_token) >= 2 and minute_token[0] in "零〇" and len(minute) < 2:
            minute = f"0{minute}"  # 九点零五分 → 9点05分
        claim(match.start(2), match.end(3), f"{hour}点{minute}")

    for match in _ORDINAL_RE.finditer(source):
        value = _parse_cardinal(match.group(1))
        if value is not None:
            claim(match.start(1), match.end(1), str(value))

    for match in _SEQ_RE.finditer(source):
        rendered = _marker_number_text(match.group(2))
        if rendered is not None:
            claim(match.start(2), match.end(2), rendered)

    for match in _SUFFIX_SEQ_RE.finditer(source):
        rendered = _marker_number_text(match.group(1))
        if rendered is not None:
            claim(match.start(1), match.end(1), rendered)

    for match in _DECIMAL_RE.finditer(source):
        integer = match.group(1)
        if _DIGIT_RE.fullmatch(integer):
            whole = int(_digits_to_ascii(integer)) if len(integer) <= 2 else None
        else:
            whole = _parse_cardinal(integer)
        if whole is None:
            continue
        claim(match.start(), match.end(), f"{whole}.{_digits_to_ascii(match.group(2))}")

    for match in _CARDINAL_RE.finditer(source):
        value = _parse_cardinal(match.group(1))
        if value is None:
            continue
        claim(match.start(), match.end(), str(value))

    # Standalone positional runs (一二三 → 123, 零零 → 00), leading zeros kept.
    # Approximate pairs (两三, 三四), AABB reduplication (三三两两), and
    # two-digit runs glued to a preceding word (纪念五四) are not codes.
    for match in _POSITIONAL_RE.finditer(source):
        token = match.group(1)
        if _approximate_digits(token) or _reduplicated_digits(token):
            continue
        if len(token) == 2 and _glued_to_cjk(source, match.start()):
            continue
        claim(match.start(), match.end(), _digits_to_ascii(token))

    for match in _MEASURE_RE.finditer(source):
        if (
            match.group(1) == "一"
            and match.start(1) > 0
            and source[match.start(1) - 1] in _DETERMINER_PREFIXES
        ):
            # 下一个 / 另一个 / 每一个: a determiner, not the quantity 1.
            continue
        measure_value = _single_number(match.group(1))
        if measure_value is None:
            continue
        claim(match.start(), match.end(), measure_value + match.group(2))

    for match in _STANDALONE_RE.finditer(source):
        standalone_value = _single_number(match.group(1))
        if standalone_value is None:
            continue
        claim(match.start(), match.end(), standalone_value)

    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source
