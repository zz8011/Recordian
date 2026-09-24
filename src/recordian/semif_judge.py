"""Ask SemIf which continuation fits the words already on screen.

SemIf only chooses among the given candidates. The preceding text is the
evidence. A weak or failed answer means do not delete anything.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_SEMIF_URL = os.environ.get("RECORDIAN_SEMIF_URL", "http://192.168.5.111:42032/v1/systemone")
_MIN_LEAD = 0.15
_MIN_TOP = 0.55
_PUNCT = set("，。！？；：、,.!?;:")


def choose_continuation(prefix: str, current_tail: str, new_tail: str) -> str | None:
    """Return ``new`` when the new tail should replace the current one.

    ``None`` means keep what is already written. ``prefix`` is the text both
    candidates follow; without that evidence SemIf is not asked.
    """
    before = str(prefix).strip()
    old = str(current_tail).strip()
    new = str(new_tail).strip()
    if len(before) < 2 or not old or not new or old == new:
        return None
    try:
        answer = _choose(before, {"keep": old, "new": new})
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return None
    return answer


def choose_punctuation(prefix: str, marks: list[str]) -> str | None:
    """Pick a punctuation mark, or ``""`` for none. ``None`` means abstain."""
    before = str(prefix).strip()
    unique: list[str] = []
    for mark in marks:
        mark = str(mark)
        if mark not in unique:
            unique.append(mark)
    if len(before) < 2 or len(unique) < 2:
        return None
    criteria: dict[str, str] = {}
    for mark in unique[:8]:
        if mark == "":
            criteria["none"] = "不加标点"
        elif all(ch in _PUNCT for ch in mark):
            criteria[mark] = mark
    if len(criteria) < 2:
        return None
    try:
        answer = _choose(
            before,
            criteria,
            instructions="前面的话已经写定。选择这里该用的标点。如果不该加标点，就选不加标点。",
        )
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return None
    if answer == "none":
        return ""
    if answer in criteria:
        return answer
    return None


def choose_user_word(prefix: str, heard: str, hotwords: list[str]) -> str:
    """Prefer the user's own word when SemIf says it fits the preceding text."""
    spoken = str(heard).strip()
    before = str(prefix).strip()
    if len(before) < 2 or not spoken:
        return spoken
    options: dict[str, str] = {spoken: spoken}
    spoken_key = _compact(spoken)
    for word in hotwords:
        word = str(word).strip()
        if not word or word == spoken or word in options:
            continue
        if _compact(word) == spoken_key:
            options[word] = word
        if len(options) >= 8:
            break
    if len(options) < 2:
        return spoken
    try:
        answer = _choose(
            before,
            options,
            instructions="前面的话已经写定。候选里有用户的常用词。选择紧接在后面最合理的一个写法。",
        )
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return spoken
    if answer in options:
        return answer
    return spoken


def trim_new_piece(prefix: str, piece: str) -> str:
    """Drop the last uncommitted character when SemIf says it does not belong yet."""
    before = str(prefix)
    extra = str(piece)
    if len(before) < 2 or len(extra) < 2:
        return extra
    shorter = extra[:-1]
    try:
        answer = _choose(before, {"place": extra, "shorter": shorter})
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return extra
    if answer == "shorter":
        return shorter
    return extra


def _compact(token: str) -> str:
    return "".join(ch for ch in token.casefold() if ch.isalnum())


def _choose(prefix: str, options: dict[str, str], instructions: str | None = None) -> str | None:
    payload = {
        "state": prefix,
        "questions": {
            "pick": {
                "type": "choice",
                "instructions": instructions or "前面的话已经写定。选择紧接在后面、最合理的一个字或词。",
                "criteria": options,
            }
        },
    }
    request = urllib.request.Request(
        _SEMIF_URL,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=0.35) as response:
        body = json.loads(response.read().decode())
    picked = body["answers"]["pick"]
    choice = str(picked.get("choice") or "")
    probabilities = picked.get("probabilities") or {}
    if choice not in options:
        return None
    ranked = sorted((float(score), name) for name, score in probabilities.items())
    if not ranked:
        return choice
    top_score, top_name = ranked[-1]
    second = ranked[-2][0] if len(ranked) > 1 else 0.0
    if top_name != choice or top_score < _MIN_TOP or top_score - second < _MIN_LEAD:
        return None
    return choice
