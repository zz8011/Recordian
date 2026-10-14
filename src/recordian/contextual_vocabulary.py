"""Bounded context and declared options for one lexicon-supported word."""

from __future__ import annotations

import re
from typing import Any

from .contextual_choice import CandidateEdit, Choice

# Preserve the existing vocabulary policy. It is calibrated separately from
# particles; unifying the executor must not silently retune either adapter.
MIN_TOP = 0.70
MIN_MARGIN = 0.20
CONTEXT_FLANK = 160
PREVIOUS_LIMIT = 256
MAX_OPTIONS = 8
_INSTRUCTIONS = "这是整句识别假设。只能选择一个候选条件。不要改条件以外的字，不要删字。"
_BOUNDARY = re.compile(r"[，。！？；：、,.!?;:\n]+")


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
    if focus.find(surface) != focus.rfind(surface):
        clause_before = _BOUNDARY.split(before)[-1]
        clause_after = _BOUNDARY.split(after)[0]
        clause = clause_before + surface + clause_after
        if clause.find(surface) == clause.rfind(surface):
            before, after = clause_before, clause_after
            focus = clause
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
