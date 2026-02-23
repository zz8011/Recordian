from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


def normalize_text(text: str) -> str:
    """Normalize text for mixed Chinese-English CER comparison."""
    if not text:
        return ""
    lowered = text.lower()
    return "".join(ch for ch in lowered if ch.isalnum())


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # Keep the DP row on the shorter string to reduce memory.
    if len(a) < len(b):
        short, long_ = a, b
    else:
        short, long_ = b, a

    prev = list(range(len(short) + 1))
    for i, ch_long in enumerate(long_, start=1):
        curr = [i]
        for j, ch_short in enumerate(short, start=1):
            insert_cost = curr[j - 1] + 1
            delete_cost = prev[j] + 1
            replace_cost = prev[j - 1] + (0 if ch_long == ch_short else 1)
            curr.append(min(insert_cost, delete_cost, replace_cost))
        prev = curr
    return prev[-1]


def char_error_rate(reference: str, hypothesis: str) -> tuple[float, int, int]:
    """Return (cer, edit_errors, reference_chars)."""
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    ref_chars = len(ref)
    errors = edit_distance(ref, hyp)

    if ref_chars == 0:
        cer = 0.0 if not hyp else 1.0
    else:
        cer = errors / ref_chars
    return cer, errors, ref_chars


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        return None
    if q <= 0:
        return min(values)
    if q >= 100:
        return max(values)

    ordered = sorted(values)
    idx = (len(ordered) - 1) * (q / 100.0)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    weight = idx - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


@dataclass(slots=True)
class GateThresholds:
    max_global_cer: float | None = None
    max_latency_p90_ms: float | None = None
    max_failure_rate: float | None = None
    max_rtf_avg: float | None = None


def evaluate_summary_gates(summary: Mapping[str, object], thresholds: GateThresholds) -> list[str]:
    """Return a list of gate failure reasons. Empty list means pass."""

    failures: list[str] = []
    _check_upper_bound(
        name="global_cer",
        value=summary.get("global_cer"),
        maximum=thresholds.max_global_cer,
        failures=failures,
    )
    _check_upper_bound(
        name="latency_ms_p90",
        value=summary.get("latency_ms_p90"),
        maximum=thresholds.max_latency_p90_ms,
        failures=failures,
    )
    _check_upper_bound(
        name="failure_rate",
        value=summary.get("failure_rate"),
        maximum=thresholds.max_failure_rate,
        failures=failures,
    )
    _check_upper_bound(
        name="rtf_avg",
        value=summary.get("rtf_avg"),
        maximum=thresholds.max_rtf_avg,
        failures=failures,
    )
    return failures


def _check_upper_bound(
    *,
    name: str,
    value: object,
    maximum: float | None,
    failures: list[str],
) -> None:
    if maximum is None:
        return
    if not isinstance(value, (int, float)):
        failures.append(f"{name}=missing (threshold <= {maximum})")
        return
    if float(value) > maximum:
        failures.append(f"{name}={float(value):.6f} > {maximum:.6f}")
