"""Deterministic hotword correction for ASR output.

Runs after ASR and before LLM refine. Two strategies:

- ASCII hotwords: variant matching that is insensitive to case, spacing and
  ``._-`` separators (``CodeX`` → ``Codex``), plus a conservative edit-distance
  pass for near-miss spellings (``SPARK`` → ``SPARC``). Edit-distance only
  applies to hotwords whose compact form is at least 6 chars, so short common
  English words (``code`` vs ``codex``) are never rewritten.
- CJK hotwords: pinyin homophone matching — a same-length window whose pinyin
  sequence is identical to the hotword's is rewritten to the canonical
  characters (``张征`` → ``张拯``). Requires ``pypinyin`` (wake extra); when it
  is not installed the CJK pass is silently skipped.

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
    for match in _ASCII_RUN_RE.finditer(text):
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


def _correct_cjk(
    text: str,
    cjk_terms: list[str],
    *,
    changes: list[tuple[str, str]],
) -> str:
    if not cjk_terms:
        return text

    # Map pinyin sequence -> canonical term (first hotword wins on collisions).
    target_by_pinyin: dict[tuple[str, ...], str] = {}
    for term in cjk_terms:
        pinyin = _pinyin_sequence(term)
        if pinyin and len(pinyin) == len(term):
            target_by_pinyin.setdefault(pinyin, term)
    if not target_by_pinyin:
        return text

    lengths = sorted({len(term) for term in target_by_pinyin.values()}, reverse=True)

    def _rewrite_run(run: str) -> str:
        result = run
        for length in lengths:
            if len(result) < length:
                continue
            offset = 0
            while offset + length <= len(result):
                window = result[offset : offset + length]
                window_pinyin = _pinyin_sequence(window)
                term = target_by_pinyin.get(window_pinyin) if window_pinyin else None
                if term is not None and len(term) == length and window != term:
                    result = result[:offset] + term + result[offset + length :]
                    changes.append((window, term))
                    offset += length
                else:
                    offset += 1
        return result

    parts: list[str] = []
    last = 0
    for match in _CJK_RUN_RE.finditer(text):
        parts.append(text[last : match.start()])
        parts.append(_rewrite_run(match.group(0)))
        last = match.end()
    parts.append(text[last:])
    return "".join(parts)


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
    """Apply explicit ``src → dst`` substitutions, longest source first."""
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
    Explicit replacements run first and win over heuristic matching.
    """
    if not text.strip():
        return text, []

    changes: list[tuple[str, str]] = []
    corrected = text
    if replacements:
        corrected, replacement_changes = apply_lexicon_replacements(corrected, replacements)
        changes.extend(replacement_changes)

    if not hotwords:
        return corrected, changes

    ascii_terms, cjk_terms = _canonical_hotwords(hotwords)
    corrected = _correct_ascii(corrected, ascii_terms, max_ascii_edits=max_ascii_edits, changes=changes)
    corrected = _correct_cjk(corrected, cjk_terms, changes=changes)
    return corrected, changes
