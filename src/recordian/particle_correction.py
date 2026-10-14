"""Optional final-only Chinese particle correction through bounded Jev choices.

Only an existing 的 can become 地 or 得. No generated prose is accepted.
The final pass shares the hotword corrector's deadline and never judges partials.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any

from .hotword_corrector import _protected_spans
from .jev_judge import request_jev_choices

MIN_TOP = 0.85
MIN_MARGIN = 0.35
MAX_QUESTIONS = 8
MAX_CLAUSE_CHARS = 64
_BOUNDARY = re.compile(r"[，。！？；：、,.!?;:\n]+")
_LITERAL = re.compile(r"[A-Za-z_`\"'“”‘’「」『』【】/\\]|改成|写成|这个字|这个词")
_STATE = "Chinese dictation orthography. Select only the one marked character; all other characters remain unchanged."
_INSTRUCTIONS = (
    "Select the most natural and grammatically correct Chinese phrase. "
    "The candidates differ only in the marked character. Use standard written Chinese. "
    "Preserve the meaning of the clause."
)


def particle_questions(text: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[int]]]:
    """Bounded clause candidates, with identical repeated clauses sharing a vote."""
    questions: dict[str, dict[str, Any]] = {}
    positions: dict[str, list[int]] = {}
    seen: dict[tuple[str, int], str] = {}
    # Quoted dictation may deliberately discuss an incorrect spelling.
    # Skip the snapshot, including clauses inside an unfinished quote.
    if any(char in text for char in "`\"'“”‘’「」『』【】"):
        return questions, positions
    protected = _protected_spans(text)
    begin = 0
    spans = []
    for match in _BOUNDARY.finditer(text):
        spans.append((begin, match.start()))
        begin = match.end()
    spans.append((begin, len(text)))
    for begin, end in spans:
        clause = text[begin:end]
        if not 4 <= len(clause) <= MAX_CLAUSE_CHARS or _LITERAL.search(clause):
            continue
        if any(begin < stop and start < end for start, stop in protected):
            continue
        for index, char in enumerate(clause):
            if char != "的" or index == 0 or index == len(clause) - 1:
                continue
            key = (clause, index)
            name = seen.get(key)
            if name is None:
                if len(questions) >= MAX_QUESTIONS:
                    continue
                name = f"p{len(questions)}"
                seen[key] = name
                questions[name] = {
                    "type": "choice",
                    "instructions": _INSTRUCTIONS,
                    "criteria": {
                        option: clause[:index] + "【" + option + "】" + clause[index + 1 :] for option in "的地得"
                    },
                }
                positions[name] = []
            positions[name].append(begin + index)
    return questions, positions


class ParticleCorrector:
    """Decorate an existing corrector; one batch at final, with shared timeout."""

    def __init__(self, base: Any, *, timeout_s: float, endpoint: str = "") -> None:
        self.base = base
        self.timeout_s = timeout_s
        self.endpoint = endpoint
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._proc: Any = None
        self._cache: OrderedDict[str, str] = OrderedDict()

    def submit(self, text: str) -> str:
        return str(self.base.submit(text))

    def poll(self, text: str) -> str:
        with self._lock:
            cached = self._cache.get(text)
        return cached if cached is not None else str(self.base.poll(text))

    def finish(self, text: str) -> str:
        deadline = time.monotonic() + max(0.0, self.timeout_s)
        with self._lock:
            if text in self._cache:
                return self._cache[text]
            generation = self._generation
        original = str(self.base.finish(text))
        questions, positions = particle_questions(original)
        with self._lock:
            if self._closed or generation != self._generation:
                return original
        remaining = deadline - time.monotonic()
        if questions and remaining >= 0.15:
            answers = request_jev_choices(
                remaining,
                _STATE,
                questions,
                endpoint=self.endpoint,
                min_top=MIN_TOP,
                min_margin=MIN_MARGIN,
                on_process=lambda proc: self._register(proc, generation),
            )
            chars = list(original)
            for name, indexes in positions.items():
                choice = answers.get(name)
                if choice not in ("地", "得"):
                    continue
                for index in indexes:
                    if chars[index] == "的":
                        chars[index] = choice
            result = "".join(chars)
        else:
            result = original
        with self._lock:
            if self._closed or generation != self._generation:
                return original
            self._proc = None
            self._cache[text] = result
            self._cache.move_to_end(text)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        return result

    def _register(self, proc: Any, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._generation:
                self._kill(proc)
            else:
                self._proc = proc

    @staticmethod
    def _kill(proc: Any) -> None:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._cache.clear()
            self._kill(self._proc)
            self._proc = None
        self.base.cancel()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()
        self.base.close()
