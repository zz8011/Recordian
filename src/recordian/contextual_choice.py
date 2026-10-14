"""Apply caller-authored contextual choices without allowing model-authored edits.

Offsets refer to the original snapshot and use half-open Unicode character
ranges. Request deadlines, cancellation, batching and confidence gates belong to
the adapter; this module neither starts a worker nor imports a model client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .hotword_corrector import _protected_spans


@dataclass(frozen=True)
class CandidateEdit:
    """One immutable replacement of a nonempty range in the input snapshot."""

    start: int
    end: int
    original: str
    replacement: str


@dataclass(frozen=True)
class Choice:
    """A question and its caller-declared option IDs and atomic edit bundles.

    An empty tuple expresses keep. The dictionaries remain caller-owned; request
    payloads are copied so request code cannot mutate the question whitelist.
    """

    question_id: str
    question: dict
    edits: dict[str, tuple[CandidateEdit, ...]]


def _index_choices(choices: Iterable[Choice]) -> dict[str, Choice]:
    indexed: dict[str, Choice] = {}
    for choice in choices:
        if choice.question_id in indexed:
            raise ValueError(f"duplicate question ID: {choice.question_id!r}")
        indexed[choice.question_id] = choice
    return indexed


def question_payload(choices: Iterable[Choice]) -> dict:
    """Return detached questions keyed by unique question ID.

    Duplicate IDs are a caller configuration error, including for keep-only
    questions. The private edit whitelist is never sent to the request.
    """
    return {qid: deepcopy(choice.question) for qid, choice in _index_choices(choices).items()}


def _valid_edit(snapshot: str, edit: CandidateEdit, protected: list[tuple[int, int]]) -> bool:
    return (
        isinstance(edit, CandidateEdit)
        and type(edit.start) is int
        and type(edit.end) is int
        and 0 <= edit.start < edit.end <= len(snapshot)
        and isinstance(edit.original, str)
        and isinstance(edit.replacement, str)
        and snapshot[edit.start : edit.end] == edit.original
        and not any(edit.start < end and start < edit.end for start, end in protected)
    )


def apply_choices(snapshot: str, choices: Iterable[Choice], answers: Mapping | None) -> str:
    """Apply only explicitly selected, valid, nonconflicting whitelist bundles.

    Malformed/unknown answers and invalid selected bundles preserve the original
    text. Every edit in a bundle must validate before any edit is accepted. Any
    overlap between distinct edits rejects all involved bundles, including their
    disjoint edits, so a selected option is never applied partially. Identical
    edits are deduplicated before conflict detection and application.
    """
    indexed = _index_choices(choices)
    if not isinstance(answers, Mapping):
        return snapshot
    protected = _protected_spans(snapshot)
    bundles: list[tuple[CandidateEdit, ...]] = []
    for qid, choice in indexed.items():
        selected = answers.get(qid)
        criteria = choice.question.get("criteria") if isinstance(choice.question, dict) else None
        if (
            not isinstance(selected, str)
            or not isinstance(criteria, Mapping)
            or not isinstance(choice.edits, Mapping)
            or selected not in criteria
            or selected not in choice.edits
        ):
            continue
        edits = choice.edits[selected]
        if isinstance(edits, tuple) and all(_valid_edit(snapshot, edit, protected) for edit in edits):
            bundles.append(edits)

    unique = {edit for bundle in bundles for edit in bundle}
    ordered = sorted(unique, key=lambda edit: (edit.start, edit.end, edit.original, edit.replacement))
    conflicts: set[CandidateEdit] = set()
    # In start order, a previous interval with the greatest end detects every
    # newly overlapping interval. Earlier overlapping intervals are already
    # marked, including chains and contained intervals.
    furthest: CandidateEdit | None = None
    for edit in ordered:
        if furthest is not None and edit.start < furthest.end:
            conflicts.update((furthest, edit))
        if furthest is None or edit.end > furthest.end:
            furthest = edit

    accepted = {edit for bundle in bundles if not conflicts.intersection(bundle) for edit in bundle}
    if not accepted:
        return snapshot
    # Slice only the immutable snapshot, visiting each unchanged range once.
    # Joining once avoids copying the growing result for every accepted edit.
    chunks: list[str] = []
    cursor = len(snapshot)
    for edit in sorted(accepted, key=lambda edit: edit.start, reverse=True):
        chunks.extend((snapshot[edit.end : cursor], edit.replacement))
        cursor = edit.start
    chunks.append(snapshot[:cursor])
    return "".join(reversed(chunks))


def judge_choices(
    snapshot: str,
    state: str,
    choices: Iterable[Choice],
    *,
    request: Callable[..., Mapping | None],
    timeout_s: float,
    **options: Any,
) -> str:
    """Call the adapter's bounded request once and apply its option IDs.

    The request runs synchronously in the caller's execution context. Exceptions
    (including timeout and cancellation) propagate to that owner. An adapter
    that returns empty answers on failure leaves the snapshot unchanged.
    """
    if not timeout_s > 0:
        return snapshot
    choices = tuple(choices)
    if not choices:
        return snapshot
    answers = request(timeout_s, state, question_payload(choices), **options)
    return apply_choices(snapshot, choices, answers)
