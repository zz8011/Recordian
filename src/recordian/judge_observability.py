"""Structured outcome events for the correction judges (observability only).

Every judgment the correction layer makes — or refuses to make — emits one
``judge_event`` line through the caller's module logger so the tray log can
be counted after the fact (``grep -c judge_event``). Before this module the
judges swallowed every outcome: timeouts, transport failures, gate rejects
and successes were indistinguishable, and nobody could tell whether the
judge ever fired at all.

This module never changes a correction decision. ``emit_judge_event``
swallows its own failures, and callers measure latency with
``time.perf_counter`` around the unchanged call. Dictation text is never
logged — only its length bucket.
"""

from __future__ import annotations

import logging


def text_len_bucket(length: int) -> str:
    """Coarse length class for a judged text; the text itself stays private."""
    if length <= 0:
        return "0"
    if length <= 20:
        return "1-20"
    if length <= 60:
        return "21-60"
    if length <= 120:
        return "61-120"
    return "121+"


def emit_judge_event(
    logger: logging.Logger,
    *,
    site: str,
    outcome: str,
    latency_ms: float | None = None,
    timeout_budget_ms: float | None = None,
    text_len: int | None = None,
    correction_applied: bool | None = None,
    extra: str = "",
) -> None:
    """Log one judge outcome. Never raises, never touches the decision path."""
    try:
        parts = [f"judge_event site={site} outcome={outcome}"]
        if latency_ms is not None:
            parts.append(f"latency_ms={latency_ms:.1f}")
        if timeout_budget_ms is not None:
            parts.append(f"timeout_budget_ms={timeout_budget_ms:.0f}")
        if text_len is not None:
            parts.append(f"text_len_bucket={text_len_bucket(text_len)}")
        if correction_applied is not None:
            parts.append(f"correction_applied={'true' if correction_applied else 'false'}")
        if extra:
            parts.append(extra)
        logger.info(" ".join(parts))
    except Exception:
        pass
