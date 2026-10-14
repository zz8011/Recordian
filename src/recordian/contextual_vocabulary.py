"""Bounded context and declared options for one lexicon-supported word."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .contextual_choice import CandidateEdit, Choice
from .hotword_corrector import _protected_spans

# Preserve the existing vocabulary policy. It is calibrated separately from
# particles; unifying the executor must not silently retune either adapter.
MIN_TOP = 0.70
MIN_MARGIN = 0.20
CONTEXT_FLANK = 160
PREVIOUS_LIMIT = 256
MAX_OPTIONS = 8
_INSTRUCTIONS = "这是整句识别假设。只能选择一个候选条件。不要改条件以外的字，不要删字。"
_OCCURRENCE_SEPARATORS = "，。！？；,:;!?\n"


def vocabulary_choice(
    text: str, span: dict[str, Any], *, context: str = "",
) -> tuple[str, Choice] | None:
    """Build one bounded choice while keeping offsets in the full snapshot."""
    start, end = span.get("start"), span.get("end")
    surface = span.get("surface")
    if (
        type(start) is not int or type(end) is not int
        or not 0 <= start < end <= len(text)
        or not isinstance(surface, str) or text[start:end] != surface
    ):
        return None
    begin, stop = max(0, start - CONTEXT_FLANK), min(len(text), end + CONTEXT_FLANK)
    before, after = text[begin:start], text[end:stop]
    focus = before + surface + after
    state = focus + f"\n只判断这个跨度：「{surface}」"
    if context:
        state = f"上一段：{context[-PREVIOUS_LIMIT:]}\n{state}"
    options = {"keep": f"原句中的{surface}在此语境正确，保持原文。{focus}"}
    edits: dict[str, tuple[CandidateEdit, ...]] = {"keep": ()}
    seen = {surface}
    for candidate in span.get("candidates", []):
        if not isinstance(candidate, str) or not candidate or len(candidate) > 32 or candidate in seen:
            continue
        seen.add(candidate)
        key = f"c{len(options) - 1}"
        options[key] = (
            f"此语境应该使用{candidate}，应把{surface}替换为{candidate}，其余不变。"
            + before + candidate + after
        )
        edits[key] = (CandidateEdit(start, end, surface, candidate),)
        if len(options) >= MAX_OPTIONS:
            break
    if len(options) == 1:
        return None
    return state, Choice(
        "pick", {"type": "choice", "instructions": _INSTRUCTIONS, "criteria": options}, edits,
    )


def vocabulary_occurrence_choices(
    text: str,
    span: dict[str, Any],
    *,
    is_eligible: Callable[[int, int], bool],
    context: str = "",
    aliases: Sequence[dict[str, Any]] = (),
) -> tuple[str, list[Choice]] | None:
    """Preserve the baseline and add independent, locally anchored positions.

    The original eligibility guards and candidate-product cap still apply.
    Ambiguous or oversized expansions keep only the unchanged baseline.
    """
    baseline = vocabulary_choice(text, span, context=context)
    if baseline is None:
        return None
    state, initial = baseline
    fallback = (state, [initial])
    start, end, surface = span["start"], span["end"], span["surface"]
    begin, stop = max(0, start - CONTEXT_FLANK), min(len(text), end + CONTEXT_FLANK)
    focus = text[begin:stop]
    if any(marker in focus or marker in context for marker in "〔〕"):
        return fallback

    # Reuse the baseline whitelist, including its order, deduplication and cap.
    words = [surface]
    words.extend(bundle[0].replacement for bundle in baseline[1].edits.values() if bundle)
    if any(len(word) != len(surface) for word in words):
        return fallback

    protected = _protected_spans(text)
    positions: list[tuple[int, int]] = []
    ordinals: dict[int, int] = {}
    occurrence_count = 0
    position = text.find(surface, begin, stop)
    while position >= 0:
        occurrence_count += 1
        position_end = position + len(surface)
        if (
            not any(position < right and left < position_end for left, right in protected)
            and is_eligible(position, position_end)
        ):
            if positions and position < positions[-1][1]:
                return fallback
            if any(position < alias["end"] and alias["start"] < position_end for alias in aliases):
                return fallback
            positions.append((position, position_end))
            # The model sees the full source, including protected occurrences.
            # Its ordinal must describe that source, not the filtered edit list.
            ordinals[position] = occurrence_count
        # Advance one character so overlapping starts cannot silently disappear.
        position = text.find(surface, position + 1, stop)
    if len(positions) <= 1 or len(words) ** len(positions) > MAX_OPTIONS:
        return fallback

    choices = [initial]
    for i, (left, right) in enumerate(positions):
        if left == start and right == end:
            continue
        lo = (positions[i - 1][1] + left) // 2 if i else begin
        hi = (right + positions[i + 1][0]) // 2 if i + 1 < len(positions) else stop
        for k in range(left - 1, lo - 1, -1):
            if text[k] in _OCCURRENCE_SEPARATORS:
                lo = k + 1
                break
        for k in range(right, hi):
            if text[k] in _OCCURRENCE_SEPARATORS:
                hi = k
                break
        before, after = text[lo:left], text[right:hi]
        options = {}
        edits: dict[str, tuple[CandidateEdit, ...]] = {}
        for j, word in enumerate(words):
            key = "keep" if j == 0 else f"c{j - 1}"
            options[key] = f"此处应使用{word}：" + before + word + after
            edits[key] = () if j == 0 else (CandidateEdit(left, right, surface, word),)
        instruction = (
            f"依据完整上下文，只判断第{ordinals[left]}处词语：{before}〔{surface}〕{after}。"
            "其他位置的用词不属于本题。"
        )
        choices.append(Choice(
            f"p{i}", {"type": "choice", "instructions": instruction, "criteria": options}, edits,
        ))
    return state, choices
