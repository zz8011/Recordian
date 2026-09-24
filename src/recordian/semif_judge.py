"""Bounded SemIf choice among caller-supplied candidates.

A missing, non-finite, non-normalized, contradictory, or weak distribution
keeps the original text. The 0.70 / 0.20 gate is a fixed conservative reject
rule so a 0.51 versus 0.49 split cannot change text. It is not a measured
accuracy rate. ``trim_new_piece`` does not delete characters or use the network.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

_PUNCT = set("，。！？；：、,.!?;:")
# Fixed conservative gate. A near tie such as 0.51/0.49 stays on the original.
_MIN_TOP = 0.70
_MIN_MARGIN = 0.20


def interpret_choice(options: Mapping[str, str], picked: object) -> str | None:
    """Return an option key only when the distribution clears the fixed gate.

    ``None`` means keep the original. A ``choice`` without a usable
    distribution is rejected. ``_MIN_TOP`` and ``_MIN_MARGIN`` reject weak
    answers; they are not a calibrated probability of being correct.
    """
    if not isinstance(picked, dict) or not options:
        return None
    choice = picked.get("choice")
    if not isinstance(choice, str) or choice not in options:
        return None
    probabilities = picked.get("probabilities")
    if not isinstance(probabilities, dict):
        return None
    if set(probabilities) != set(options):
        return None
    scores: dict[str, float] = {}
    for name, raw in probabilities.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        score = float(raw)
        if not math.isfinite(score) or score < 0.0:
            return None
        scores[str(name)] = score
    total = sum(scores.values())
    if abs(total - 1.0) > 1e-3:
        return None
    ranking = sorted(scores.items(), key=lambda item: item[1])
    top_name, top_score = ranking[-1]
    second = ranking[-2][1] if len(ranking) > 1 else 0.0
    if top_name != choice or top_score < _MIN_TOP or top_score - second < _MIN_MARGIN:
        return None
    return choice


def require_requests() -> Any:
    """Import requests only when a call is actually enabled.

    A missing install raises ImportError. Callers must not treat that as a
    quiet decision to keep the original text.
    """
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            "启用 SemIf 需要可选依赖 requests。请安装 correction extra 后再打开端点。"
        ) from exc
    return requests


def request_choice(
    session: Any,
    endpoint: str,
    timeout_s: float,
    state: str,
    options: dict[str, str],
    *,
    instructions: str,
) -> str | None:
    """POST one choice. Network and payload failures return None."""
    if session is None or not endpoint or timeout_s <= 0 or len(options) < 2:
        return None
    requests = require_requests()
    payload = {
        "state": state,
        "questions": {
            "pick": {
                "type": "choice",
                "instructions": instructions,
                "criteria": options,
            }
        },
    }
    try:
        response = session.post(endpoint, json=payload, timeout=timeout_s)
        response.raise_for_status()
        body = response.json()
        picked = body["answers"]["pick"]
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError):
        return None
    return interpret_choice(options, picked)


def request_choices(
    session: Any,
    endpoint: str,
    timeout_s: float,
    state: str,
    questions: dict[str, dict[str, Any]],
) -> dict[str, str | None]:
    """POST several named choice questions in one bounded request.

    Each question is a ``{"type": "choice", "instructions": ..., "criteria":
    {...}}`` mapping; every answer passes the same fixed ``interpret_choice``
    gate, so a missing or weak answer keeps the original for that question.
    Network and payload failures return an empty dict.
    """
    if session is None or not endpoint or timeout_s <= 0 or not questions:
        return {}
    requests = require_requests()
    payload = {"state": state, "questions": questions}
    try:
        response = session.post(endpoint, json=payload, timeout=timeout_s)
        response.raise_for_status()
        answers = response.json()["answers"]
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(answers, dict):
        return {}
    results: dict[str, str | None] = {}
    for name, question in questions.items():
        criteria = question.get("criteria")
        if not isinstance(criteria, dict):
            continue
        results[name] = interpret_choice(criteria, answers.get(name))
    return results


class SemIfClient:
    """Reusable session. Empty endpoint or ``enabled=False`` never connects."""

    def __init__(
        self,
        endpoint: str = "",
        *,
        timeout_s: float = 0.12,
        enabled: bool = False,
        session: Any | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.enabled = enabled
        self._owns_session = session is None
        self._session = session
        self._closed = False

    def choose(
        self,
        state: str,
        options: dict[str, str],
        *,
        instructions: str | None = None,
    ) -> str | None:
        if self._closed or not self.enabled or not self.endpoint:
            return None
        if self._session is None:
            requests = require_requests()
            self._session = requests.Session()
        return request_choice(
            self._session,
            self.endpoint,
            self.timeout_s,
            state,
            options,
            instructions=instructions or "只能从给定候选里选一个，或保留原文。不要改候选以外的字。",
        )

    def close(self) -> None:
        """Close an owned session once. Unused, disabled, repeated, or caller-owned closes do nothing."""
        if self._closed:
            return
        self._closed = True
        session = self._session
        if not self._owns_session or session is None:
            return
        self._session = None
        session.close()


def choose_continuation(
    prefix: str,
    current_tail: str,
    new_tail: str,
    *,
    endpoint: str = "",
    timeout_s: float = 0.12,
    enabled: bool = False,
    session: Any | None = None,
) -> str | None:
    """Return ``new_tail`` only for an explicit enabled call with a valid choice.

    The default keeps the current tail and does not touch the network.
    """
    before = str(prefix).strip()
    old = str(current_tail).strip()
    new = str(new_tail).strip()
    if len(before) < 2 or not old or not new or old == new or not enabled or not endpoint:
        return None
    client = SemIfClient(endpoint, timeout_s=timeout_s, enabled=True, session=session)
    try:
        answer = client.choose(before, {"keep": old, "new": new})
    finally:
        if session is None:
            client.close()
    if answer == "new":
        return new
    return None


def choose_punctuation(
    prefix: str,
    marks: list[str],
    *,
    endpoint: str = "",
    timeout_s: float = 0.12,
    enabled: bool = False,
    session: Any | None = None,
) -> str | None:
    """Pick a mark, or ``''`` for none. ``None`` means abstain. Default is abstain."""
    before = str(prefix).strip()
    if not enabled or not endpoint or len(before) < 2:
        return None
    criteria: dict[str, str] = {}
    for mark in marks:
        mark = str(mark)
        if mark in criteria or len(criteria) >= 8:
            continue
        if mark == "":
            criteria["none"] = "不加标点"
        elif all(ch in _PUNCT for ch in mark):
            criteria[mark] = mark
    if len(criteria) < 2:
        return None
    client = SemIfClient(endpoint, timeout_s=timeout_s, enabled=True, session=session)
    try:
        answer = client.choose(
            before,
            criteria,
            instructions="前面的话已经写定。选择这里该用的标点。如果不该加标点，就选不加标点。",
        )
    finally:
        if session is None:
            client.close()
    if answer == "none":
        return ""
    if answer in criteria:
        return answer
    return None


def choose_user_word(
    prefix: str,
    heard: str,
    hotwords: list[str],
    *,
    endpoint: str = "",
    timeout_s: float = 0.12,
    enabled: bool = False,
    session: Any | None = None,
) -> str:
    """Return ``heard`` unless an explicit enabled call accepts another candidate."""
    spoken = str(heard).strip()
    before = str(prefix).strip()
    if not spoken or not enabled or not endpoint or len(before) < 2:
        return spoken
    options: dict[str, str] = {"keep": spoken}
    spoken_key = _compact(spoken)
    for word in hotwords:
        word = str(word).strip()
        if not word or word == spoken or word in options.values():
            continue
        if _compact(word) == spoken_key:
            options[f"c{len(options) - 1}"] = word
        if len(options) >= 8:
            break
    if len(options) < 2:
        return spoken
    client = SemIfClient(endpoint, timeout_s=timeout_s, enabled=True, session=session)
    try:
        answer = client.choose(
            before,
            options,
            instructions="前面的话已经写定。候选里有用户的常用词。选择紧接在后面最合理的一个写法。保留原文也可以。",
        )
    finally:
        if session is None:
            client.close()
    if answer in options and answer != "keep":
        return options[answer]
    return spoken


def trim_new_piece(prefix: str, piece: str) -> str:
    """Return ``piece`` unchanged.

    Dropping the last character on a model hunch changes the recognized words.
    This helper no longer calls SemIf.
    """
    del prefix
    return str(piece)


def _compact(token: str) -> str:
    return "".join(ch for ch in token.casefold() if ch.isalnum())
