"""Bounded context and declared options for one lexicon-supported word."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import product
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
_OCCURRENCE_INSTRUCTIONS = "判断哪个候选句在完整上下文中各处用词都正确。保留或替换须符合各处语义，其他文字不变。"


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


def vocabulary_occurrence_choice(
    text: str,
    span: dict[str, Any],
    *,
    is_eligible: Callable[[int, int], bool],
    context: str = "",
    aliases: Sequence[dict[str, Any]] = (),
) -> tuple[str, Choice] | None:
    """Offer complete assignments in the original span's bounded window.

    The caller supplies the same semantic/meta guards used to select the first
    pinyin span. Literal protection is applied here too. Any ambiguous or
    oversized expansion returns the unmodified single-span baseline.
    """
    baseline = vocabulary_choice(text, span, context=context)
    if baseline is None:
        return None
    start, end, surface = span["start"], span["end"], span["surface"]
    begin, stop = max(0, start - CONTEXT_FLANK), min(len(text), end + CONTEXT_FLANK)
    focus = text[begin:stop]
    if any(marker in focus or marker in context for marker in "〔〕"):
        return baseline

    # Reuse the baseline whitelist, including its order, deduplication and cap.
    words = [surface]
    words.extend(bundle[0].replacement for bundle in baseline[1].edits.values() if bundle)
    if any(len(word) != len(surface) for word in words):
        return baseline

    protected = _protected_spans(text)
    positions: list[tuple[int, int]] = []
    position = text.find(surface, begin, stop)
    while position >= 0:
        position_end = position + len(surface)
        if (
            not any(position < right and left < position_end for left, right in protected)
            and is_eligible(position, position_end)
        ):
            if positions and position < positions[-1][1]:
                return baseline
            if any(position < alias["end"] and alias["start"] < position_end for alias in aliases):
                return baseline
            positions.append((position, position_end))
        # Advance one character so overlapping starts cannot silently disappear.
        position = text.find(surface, position + 1, stop)
    if len(positions) <= 1 or len(words) ** len(positions) > MAX_OPTIONS:
        return baseline

    state = focus
    if context:
        state = f"上一段：{context[-PREVIOUS_LIMIT:]}\n{state}"

    options = {}
    edits: dict[str, tuple[CandidateEdit, ...]] = {}
    for index, assignment in enumerate(product(words, repeat=len(positions))):
        key = "keep" if index == 0 else f"c{index - 1}"
        chunks: list[str] = []
        cursor = begin
        for (left, right), word in zip(positions, assignment, strict=True):
            chunks.extend((text[cursor:left], word))
            cursor = right
        chunks.append(text[cursor:stop])
        options[key] = "".join(chunks)
        edits[key] = tuple(
            CandidateEdit(left, right, surface, word)
            for (left, right), word in zip(positions, assignment, strict=True)
            if word != surface
        )
    return state, Choice(
        "pick", {
            "type": "choice",
            "instructions": _OCCURRENCE_INSTRUCTIONS,
            "criteria": options,
        }, edits,
    )
