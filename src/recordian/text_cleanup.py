"""Text processing utilities for transcription output.

Provides helper functions for:
- Computing incremental deltas between partial transcription results.
- Optimistic first-partial detection for low-latency streaming commit.
- Normalizing final transcription text (stutter/repeat removal, trimming).
"""
from __future__ import annotations


def _append_only_delta(previous: str, current: str) -> tuple[str, str]:
    """Return *only* the new portion of *current* when it is a strict
    append-only extension of *previous*.

    Parameters
    ----------
    previous:
        The prior accumulated text (may be empty).
    current:
        The newly received text.

    Returns
    -------
    tuple[str, str]
        A ``(base, delta)`` pair where *base* is the current best
        accumulated text and *delta* is the newly appended portion (empty
        string when there is no append-only relationship).
    """
    if current == previous:
        return previous, ""
    if current.startswith(previous):
        return current, current[len(previous):]
    if previous.startswith(current):
        return previous, ""
    return previous, ""


def _stable_prefix_delta(
    *,
    previous_hypothesis: str,
    committed_text: str,
    current_hypothesis: str,
) -> tuple[str, str]:
    """Determine new stable text by comparing the previous and current
    hypotheses and keeping the longest common prefix.

    Parameters
    ----------
    previous_hypothesis:
        The hypothesis from the previous ASR partial result.
    committed_text:
        Text that has already been committed (streamed to the user).
    current_hypothesis:
        The hypothesis from the current ASR partial result.

    Returns
    -------
    tuple[str, str]
        ``(new_committed, delta)`` where *new_committed* is the updated
        committed text and *delta* is the portion that should be appended
        to the output.
    """
    previous = str(previous_hypothesis)
    committed = str(committed_text)
    current = str(current_hypothesis)
    if not current:
        return committed, ""
    if not previous:
        return committed, ""
    prefix_len = 0
    limit = min(len(previous), len(current))
    while prefix_len < limit and previous[prefix_len] == current[prefix_len]:
        prefix_len += 1
    if prefix_len <= len(committed):
        return committed, ""
    stable = current[:prefix_len]
    if stable.startswith(committed):
        return stable, stable[len(committed):]
    return committed, ""


def _revision_delta(committed: str, hypothesis: str) -> tuple[int, str]:
    """Return ``(backspace_count, text_to_type)`` to turn *committed* into *hypothesis*.

    Used for live dictation: type the latest ASR hypothesis immediately, then
    delete the diverging tail and retype when a later partial revises it.
    """
    previous = str(committed)
    current = str(hypothesis)
    if current == previous:
        return 0, ""
    prefix_len = 0
    limit = min(len(previous), len(current))
    while prefix_len < limit and previous[prefix_len] == current[prefix_len]:
        prefix_len += 1
    return len(previous) - prefix_len, current[prefix_len:]


def _optimistic_first_partial(text: str) -> str:
    """Strip a trailing punctuation mark from the first partial result so
    it can be optimistically committed before the full utterance is
    complete.

    Parameters
    ----------
    text:
        Raw first partial transcription.

    Returns
    -------
    str
        The cleaned candidate text (may be unchanged).
    """
    candidate = str(text).strip()
    if len(candidate) > 1 and candidate[-1] in "，。！？；：、,.!?;:":
        candidate = candidate[:-1]
    return candidate.strip()


def _normalize_final_text(text: str) -> str:
    """Normalize a final transcription result by removing duplicated
    halves and trailing repeated segments.

    This handles common ASR artefacts where the model outputs the same
    content twice (full-text duplication or trailing segment repetition).

    Parameters
    ----------
    text:
        Raw final transcription text.

    Returns
    -------
    str
        Cleaned and trimmed transcription text.
    """
    normalized = text.strip()
    if not normalized:
        return ""
    # Remove exact full-text duplication (e.g. "hello worldhello world" → "hello world")
    while True:
        half = len(normalized) // 2
        if len(normalized) % 2 == 0 and half > 0 and normalized[:half] == normalized[half:]:
            normalized = normalized[:half]
            continue
        break
    # Remove trailing repeated segment (up to 16 chars)
    max_tail = min(16, len(normalized) // 2)
    for tail in range(max_tail, 1, -1):
        seg = normalized[-tail:]
        if normalized.endswith(seg + seg):
            normalized = normalized[:-tail]
            break
    return normalized


def wrap_overlay_caption(text: str, *, max_chars: int = 22, max_lines: int = 3) -> str:
    """Fit live ASR text into a short overlay caption, keeping the newest tail."""
    candidate = str(text).strip()
    if not candidate:
        return ""
    max_chars = max(8, int(max_chars))
    max_lines = max(1, int(max_lines))
    budget = max_chars * max_lines
    if len(candidate) > budget:
        candidate = "…" + candidate[-(budget - 1) :]
    lines = [candidate[index : index + max_chars] for index in range(0, len(candidate), max_chars)]
    return "\n".join(lines[:max_lines])
