"""Deterministic hotword correction for ASR output.

Runs after ASR and before LLM refine. Two strategies:

- ASCII hotwords: variant matching that is insensitive to case, spacing and
  ``._-`` separators (``CodeX`` → ``Codex``), plus a conservative edit-distance
  pass for near-miss spellings (``SPARK`` → ``SPARC``). Edit-distance only
  applies to hotwords whose compact form is at least 6 chars, so short common
  English words (``code`` vs ``codex``) are never rewritten.
- CJK hotwords are not rewritten from toneless pinyin. ``时期`` must not
  replace ``十七`` or ``湿气``. Same-pinyin windows are only reported as
  ambiguous spans for a later bounded judge. Explicit ``错词→正词`` still
  applies immediately, outside protected numbers, URLs, code, and negation.

Every replacement is returned so callers can log/inspect what changed.
"""

from __future__ import annotations

import re

_ASCII_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[ ._-][A-Za-z0-9]+)*")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_CJK_CHAR_RE = re.compile(r"^[一-鿿]+$")

# Conservative boundaries for what we consider a correctable hotword.
_MIN_TERM_LEN = 2
_MAX_TERM_LEN = 32
# Edit-distance is only allowed for compact ASCII keys of this length or more.
_ASCII_EDIT_MIN_LEN = 6
# CJK homophone matching is bounded to sane hotword lengths.
_CJK_MIN_LEN = 2
_CJK_MAX_LEN = 8
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_BACKTICK_RE = re.compile(r"`[^`\n]+`")
_CODE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\([^)\n]*\)|\b[A-Za-z]+_[A-Za-z0-9_]+\b"
)
_LATIN_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_CJK_NUM_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]{2,}")
_NEGATION_RE = re.compile(
    r"(?:不要|不是|没有|不可以|不会|不能|并未|并不|别|不|没)\s*"
    r"([A-Za-z][A-Za-z0-9]*(?:[ ._-][A-Za-z0-9]+)*|[一-鿿]{1,8})"
)


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < stop and begin < end for begin, stop in spans)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Ranges that deterministic replacement and pinyin candidates must not touch."""
    spans: list[tuple[int, int]] = []
    for pattern in (_FENCE_RE, _BACKTICK_RE, _URL_RE, _EMAIL_RE, _CODE_RE, _LATIN_NUM_RE, _CJK_NUM_RE):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    spans.extend((match.start(1), match.end(1)) for match in _NEGATION_RE.finditer(text))
    return spans


def _ascii_bounded(text: str, start: int, end: int) -> bool:
    before_ok = start == 0 or not text[start - 1].isalnum()
    after_ok = end == len(text) or not text[end].isalnum()
    return before_ok and after_ok


def _hotword_variant_key(token: str) -> str:
    return re.sub(r"[\s._-]+", "", str(token).casefold())


def _hotword_preference_rank(token: str) -> tuple[int, int, str]:
    text = str(token)
    compact = re.sub(r"[\s._-]+", "", text)
    has_upper = any(ch.isalpha() and ch.isupper() for ch in text)
    has_lower = any(ch.isalpha() and ch.islower() for ch in text)
    if compact == text and has_upper and has_lower:
        bucket = 0
    elif compact == text and has_upper:
        bucket = 1
    elif compact == text:
        bucket = 2
    else:
        bucket = 3
    return (bucket, len(text), text)


def _levenshtein_at_most(a: str, b: str, max_edits: int) -> int:
    """Levenshtein distance with early exit; returns max_edits + 1 when exceeded."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_edits:
        return max_edits + 1
    if not a or not b:
        return max(len(a), len(b))

    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, ch_b in enumerate(b, start=1):
            cost = 0 if ch_a == ch_b else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min > max_edits:
            return max_edits + 1
        previous = current
    return previous[-1]


def _pinyin_sequence(text: str) -> tuple[str, ...] | None:
    """Return lowercase pinyin syllables for a CJK string, None if unavailable."""
    try:
        from pypinyin import Style, lazy_pinyin
    except ModuleNotFoundError:
        return None
    try:
        return tuple(syllable.lower() for syllable in lazy_pinyin(text, style=Style.NORMAL, errors="ignore"))
    except Exception:
        return None


def _canonical_hotwords(hotwords: list[str]) -> tuple[list[str], list[str]]:
    """Dedupe hotwords by variant key and split into (ascii, cjk) canonical forms."""
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for raw in hotwords:
        token = str(raw).strip()
        if not token or len(token) < _MIN_TERM_LEN or len(token) > _MAX_TERM_LEN:
            continue
        key = _hotword_variant_key(token)
        if not key:
            continue
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(token)

    ascii_terms: list[str] = []
    cjk_terms: list[str] = []
    for key in order:
        canonical = min(grouped[key], key=_hotword_preference_rank)
        if canonical.isascii():
            ascii_terms.append(canonical)
        elif _CJK_CHAR_RE.match(canonical) and _CJK_MIN_LEN <= len(canonical) <= _CJK_MAX_LEN:
            cjk_terms.append(canonical)
    return ascii_terms, cjk_terms


def _allowed_ascii_edits(compact_key: str, max_ascii_edits: int) -> int:
    if max_ascii_edits <= 0:
        return 0
    if len(compact_key) < _ASCII_EDIT_MIN_LEN:
        return 0
    return max_ascii_edits


def _correct_ascii(
    text: str,
    ascii_terms: list[str],
    *,
    max_ascii_edits: int,
    changes: list[tuple[str, str]],
) -> str:
    if not ascii_terms:
        return text

    targets = [(_hotword_variant_key(term), term) for term in ascii_terms]
    replacements: list[tuple[int, int, str]] = []
    protected = _protected_spans(text)
    for match in _ASCII_RUN_RE.finditer(text):
        if _overlaps(match.start(), match.end(), protected):
            continue
        candidate = match.group(0)
        candidate_key = _hotword_variant_key(candidate)
        if not candidate_key:
            continue
        for target_key, canonical in targets:
            if candidate == canonical:
                break
            if candidate_key == target_key:
                # Case / spacing / separator variant of a known hotword.
                replacements.append((match.start(), match.end(), canonical))
                changes.append((candidate, canonical))
                break
            allowed = _allowed_ascii_edits(target_key, max_ascii_edits)
            if allowed and _levenshtein_at_most(candidate_key, target_key, allowed) <= allowed:
                replacements.append((match.start(), match.end(), canonical))
                changes.append((candidate, canonical))
                break

    for start, end, canonical in reversed(replacements):
        text = text[:start] + canonical + text[end:]
    return text


def ambiguous_pinyin_span(
    text: str,
    hotwords: list[str],
    *,
    frequencies: dict[str, int] | None = None,
) -> dict[str, object] | None:
    """Return one ambiguous CJK span, or None.

    Toneless pinyin only ranks candidates. It never selects a replacement.
    Frequency is a sort key, not evidence that a candidate is correct.
    The leftmost unprotected window wins; longer hotwords win at that offset.
    """
    freq = frequencies or {}
    order: dict[str, int] = {}
    by_pinyin: dict[tuple[str, ...], list[str]] = {}
    for raw in hotwords:
        term = str(raw).strip()
        if term in order or not _CJK_CHAR_RE.match(term):
            continue
        if not _CJK_MIN_LEN <= len(term) <= _CJK_MAX_LEN:
            continue
        pinyin = _pinyin_sequence(term)
        if not pinyin or len(pinyin) != len(term):
            continue
        order[term] = len(order)
        by_pinyin.setdefault(pinyin, []).append(term)
    if not by_pinyin:
        return None

    protected = _protected_spans(text)
    lengths = sorted({len(term) for term in order}, reverse=True)
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        origin = match.start()
        for offset in range(len(run)):
            for length in lengths:
                if offset + length > len(run):
                    continue
                start = origin + offset
                end = start + length
                if _overlaps(start, end, protected):
                    continue
                window = text[start:end]
                window_pinyin = _pinyin_sequence(window)
                pooled = by_pinyin.get(window_pinyin) if window_pinyin else None
                if not pooled:
                    continue
                candidates = [term for term in pooled if term != window]
                if not candidates:
                    continue
                candidates.sort(key=lambda term: (-int(freq.get(term, 0)), order[term]))
                return {
                    "start": start,
                    "end": end,
                    "surface": window,
                    "candidates": candidates[:7],
                }
    return None


_REPLACEMENT_RE = re.compile(r"^\s*(.+?)\s*(?:->|→|=>|＝>)\s*(.+?)\s*$")
_LEXICON_SPLIT_RE = re.compile(r"[,，、;；]+")


def _is_lexicon_term(token: str) -> bool:
    text = token.strip()
    if len(text) < _MIN_TERM_LEN or len(text) > _MAX_TERM_LEN:
        return False
    if text.isdigit():
        return False
    if any(ch in text for ch in "。！？\n"):
        return False
    if text.count(" ") >= 3:
        return False
    return True


def _looks_like_sentence(line: str) -> bool:
    if any(ch in line for ch in "。！？"):
        return True
    if _LEXICON_SPLIT_RE.search(line):
        return False
    cjk = sum(1 for ch in line if "\u4e00" <= ch <= "\u9fff")
    if cjk >= 12:
        return True
    if len(line) > 40:
        return True
    return False


def _parse_replacement_pair(raw: object) -> tuple[str, str] | None:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        src = str(raw[0]).strip()
        dst = str(raw[1]).strip()
        if _is_lexicon_term(src) and _is_lexicon_term(dst) and src != dst:
            return src, dst
        return None
    match = _REPLACEMENT_RE.match(str(raw or ""))
    if match is None:
        return None
    src = match.group(1).strip()
    dst = match.group(2).strip()
    if _is_lexicon_term(src) and _is_lexicon_term(dst) and src != dst:
        return src, dst
    return None


def parse_user_lexicon(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse a user-facing lexicon blob into hotwords and explicit replacements.

    ``张征 → 张拯`` / ``CodeX -> Codex`` lines become replacements. Remaining
    comma/newline-separated short tokens become hotwords. Long prompt sentences
    are ignored so ASR context paragraphs do not pollute the correction list.
    """
    hotwords: list[str] = []
    replacements: list[tuple[str, str]] = []
    seen_hotwords: set[str] = set()
    seen_replacements: set[tuple[str, str]] = set()

    def _add_hotword(token: str) -> None:
        if token in seen_hotwords or not _is_lexicon_term(token):
            return
        seen_hotwords.add(token)
        hotwords.append(token)

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if _looks_like_sentence(line):
            continue
        parts = _LEXICON_SPLIT_RE.split(line) if _LEXICON_SPLIT_RE.search(line) else [line]
        for part in parts:
            token = part.strip()
            if not token:
                continue
            pair = _parse_replacement_pair(token)
            if pair is not None:
                if pair not in seen_replacements:
                    seen_replacements.add(pair)
                    replacements.append(pair)
                _add_hotword(pair[1])
                continue
            _add_hotword(token)
    return hotwords, replacements


def apply_lexicon_replacements(
    text: str,
    replacements: list[tuple[str, str]],
) -> tuple[str, list[tuple[str, str]]]:
    """Apply explicit ``src → dst`` substitutions, longest source first.

    This raw helper does not know about protected spans. ``correct_hotwords``
    is the entry that skips numbers, URLs, code, and negation.
    """
    if not text or not replacements:
        return text, []

    changes: list[tuple[str, str]] = []
    updated = text
    ordered = sorted(replacements, key=lambda pair: len(pair[0]), reverse=True)
    for src, dst in ordered:
        if not src or src == dst or src not in updated:
            continue
        if src.isascii():
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(src)}(?![A-Za-z0-9])")
            next_text, count = pattern.subn(dst, updated)
        else:
            count = updated.count(src)
            next_text = updated.replace(src, dst) if count else updated
        if count:
            updated = next_text
            changes.append((src, dst))
    return updated, changes


def _replace_outside_protected(
    text: str,
    replacements: list[tuple[str, str]],
) -> tuple[str, list[tuple[str, str]]]:
    """Explicit replacements that skip protected spans. Longest source first."""
    if not text or not replacements:
        return text, []
    changes: list[tuple[str, str]] = []
    updated = text
    ordered = sorted(replacements, key=lambda pair: len(pair[0]), reverse=True)
    for src, dst in ordered:
        if not src or src == dst:
            continue
        search_from = 0
        while True:
            index = updated.find(src, search_from)
            if index < 0:
                break
            end = index + len(src)
            blocked = _overlaps(index, end, _protected_spans(updated))
            if src.isascii() and not _ascii_bounded(updated, index, end):
                blocked = True
            if blocked:
                search_from = index + 1
                continue
            updated = updated[:index] + dst + updated[end:]
            changes.append((src, dst))
            search_from = index + len(dst)
    return updated, changes


def lexicon_from_args(args: object) -> tuple[list[str], list[tuple[str, str]]]:
    """Collect manual hotwords and replacements from CLI/config/常用词文本."""
    hotwords: list[str] = []
    replacements: list[tuple[str, str]] = []
    seen_hotwords: set[str] = set()
    seen_replacements: set[tuple[str, str]] = set()

    def _add_hotword(token: object) -> None:
        text = str(token).strip()
        if not text or text in seen_hotwords or not _is_lexicon_term(text):
            return
        seen_hotwords.add(text)
        hotwords.append(text)

    def _add_replacement(pair: tuple[str, str]) -> None:
        if pair in seen_replacements:
            return
        seen_replacements.add(pair)
        replacements.append(pair)
        _add_hotword(pair[1])

    for raw in getattr(args, "hotword", []) or []:
        _add_hotword(raw)

    context_hotwords, context_replacements = parse_user_lexicon(str(getattr(args, "asr_context", "") or ""))
    for token in context_hotwords:
        _add_hotword(token)
    for pair in context_replacements:
        _add_replacement(pair)

    for raw in getattr(args, "hotword_replacement", None) or getattr(args, "hotword_replacements", None) or []:
        pair = _parse_replacement_pair(raw)
        if pair is not None:
            _add_replacement(pair)

    return hotwords, replacements


def compose_effective_hotwords(args: object, auto_lexicon: object | None = None) -> list[str]:
    hotwords, _replacements = lexicon_from_args(args)
    compose = getattr(auto_lexicon, "compose_hotwords", None)
    if callable(compose):
        try:
            return list(compose(hotwords))
        except Exception:
            return hotwords
    return hotwords


def correct_hotwords(
    text: str,
    hotwords: list[str],
    *,
    max_ascii_edits: int = 1,
    replacements: list[tuple[str, str]] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite near-miss variants of hotwords in *text* to their canonical form.

    Returns ``(corrected_text, changes)`` where *changes* is a list of
    ``(original, replacement)`` pairs in the order they were applied.
    Explicit replacements run first. ASCII case, spacing, and limited
    edit-distance still apply. Toneless CJK pinyin never rewrites text;
    use :func:`ambiguous_pinyin_span` when a later judge needs candidates.
    Numbers, URLs, code, and the token after a negation are left unchanged.
    """
    if not text.strip():
        return text, []

    changes: list[tuple[str, str]] = []
    corrected = text
    if replacements:
        corrected, replacement_changes = _replace_outside_protected(corrected, replacements)
        changes.extend(replacement_changes)

    if not hotwords:
        return corrected, changes

    ascii_terms, _cjk_terms = _canonical_hotwords(hotwords)
    corrected = _correct_ascii(corrected, ascii_terms, max_ascii_edits=max_ascii_edits, changes=changes)
    return corrected, changes
