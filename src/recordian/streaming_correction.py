"""Non-blocking hotword correction for one whole-sentence ASR hypothesis.

``submit`` returns deterministic edits immediately and is idempotent for the
same original text. ``finish`` accepts a final sentence that was never
submitted and waits at most one ``timeout_s``. Disabled mode does not start
a thread and does not call the network.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, cast

from .hotword_corrector import (
    _parse_replacement_pair,
    ambiguous_pinyin_span,
    correct_hotwords,
)
from .semif_judge import request_choice

_CACHE_LIMIT = 32
_LEXICON_LIMIT = 64
_MAX_JUDGE_OPTIONS = 8
_INSTRUCTIONS = "这是整句识别假设。只能选择一个候选条件。不要改条件以外的字，不要删字。"


def _criterion(sentence: str, surface: str, candidate: str | None, start: int, end: int) -> str:
    """Describe one allowed edit. The value is not spliced back into the text."""
    if candidate is None:
        return f"原句中的{surface}在此语境正确，保持原文。{sentence}"
    replaced = sentence[:start] + candidate + sentence[end:]
    return f"此语境应该使用{candidate}，应把{surface}替换为{candidate}，其余不变。{replaced}"


class _Job:
    def __init__(
        self,
        req_id: int,
        generation: int,
        epoch: int,
        origin: str,
        deterministic: str,
        span: dict[str, object],
    ) -> None:
        self.req_id = req_id
        self.generation = generation
        self.epoch = epoch
        self.origin = origin
        self.deterministic = deterministic
        self.span = span
        self.dropped = False


def _compile_hotwords(
    hotwords: list[str],
) -> tuple[list[str], list[tuple[str, str]], dict[str, int]]:
    """Keep explicit aliases first, then at most 64 entries total.

    One SemIf call still sees at most 8 options. This limit is the lexicon
    bound, not the per-judgment candidate cap.
    """
    replacements: list[tuple[str, str]] = []
    plain: list[str] = []
    frequencies: dict[str, int] = {}
    for raw in hotwords:
        item = str(raw).strip()
        if not item:
            continue
        pair = _parse_replacement_pair(item)
        if pair is not None:
            replacements.append(pair)
            frequencies[pair[1]] = frequencies.get(pair[1], 0) + 1
        else:
            plain.append(item)
            frequencies[item] = frequencies.get(item, 0) + 1
    if len(replacements) > _LEXICON_LIMIT:
        replacements = replacements[:_LEXICON_LIMIT]
    room = _LEXICON_LIMIT - len(replacements)
    unique_plain: list[str] = []
    seen: set[str] = set()
    for term in plain:
        if term in seen:
            continue
        seen.add(term)
        unique_plain.append(term)
    if len(unique_plain) > room:
        ranked = sorted(enumerate(unique_plain), key=lambda item: (-frequencies.get(item[1], 0), item[0]))
        unique_plain = [term for _, term in ranked[:room]]
    terms = [dst for _src, dst in replacements]
    terms.extend(unique_plain)
    return terms, replacements, frequencies


class StreamingHotwordCorrector:
    def __init__(
        self,
        hotwords: list[str],
        *,
        endpoint: str = "",
        timeout_s: float = 0.12,
        enabled: bool = False,
        session: Any | None = None,
    ) -> None:
        self._terms, self._replacements, self._frequencies = _compile_hotwords(hotwords)
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._enabled = enabled
        self._owns_session = session is None
        self._session = session
        self._cv = threading.Condition()
        self._pending: _Job | None = None
        self._inflight: _Job | None = None
        self._accepted_id = 0
        self._next_id = 0
        self._generation = 0
        self._epoch = 0
        self._last_origin: str | None = None
        self._cache: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self._closed = False
        self._stopped = False
        self._thread: threading.Thread | None = None

    def submit(self, text: str) -> str:
        """Return the deterministic snapshot.

        The same original text does not cancel an in-flight judgment or drop
        a ready result. A different text replaces the single pending request.
        """
        deterministic, span = self._prepare(text)
        with self._cv:
            if self._closed:
                return deterministic
            if self._ready_cached_locked(text) or self._same_snapshot_locked(text):
                return deterministic
            if span is not None:
                from .semif_judge import require_requests

                require_requests()
            self._epoch += 1
            self._last_origin = text
            self._drop_pending_locked()
            if self._inflight is not None:
                self._inflight.dropped = True
            if span is None:
                self._cv.notify_all()
                return deterministic
            self._next_id += 1
            job = _Job(self._next_id, self._generation, self._epoch, text, deterministic, span)
            self._pending = job
            self._accepted_id = job.req_id
            self._ensure_thread_locked()
            self._cv.notify()
        return deterministic

    def poll(self, text: str) -> str:
        """Return a ready correction for this original snapshot, without waiting."""
        with self._cv:
            cached = self._cache.get(text)
            if cached is not None and cached[0] == self._generation:
                return cached[1]
        return self._deterministic(text)

    def finish(self, text: str) -> str:
        """Submit this final sentence if needed, then wait at most one budget.

        A sentence that never went through ``submit`` is still judged once.
        Repeating ``finish`` or ``submit`` on that same text does not start
        another request. Disabled mode returns the deterministic text.
        """
        deterministic = self.submit(text)
        if not self._network_on():
            return deterministic
        deadline = time.monotonic() + max(0.0, self._timeout_s)
        with self._cv:
            generation = self._generation
            epoch = self._epoch
            while True:
                cached = self._cache.get(text)
                if cached is not None and cached[0] == generation:
                    return cached[1]
                if self._closed or self._generation != generation:
                    return deterministic
                if not self._job_matches_locked(text, generation, epoch):
                    return deterministic
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._drop_matching_locked(text, generation, epoch)
                    return deterministic
                self._cv.wait(remaining)

    def cancel(self) -> None:
        """Drop current results and close the owned session. Does not block."""
        old_session = None
        with self._cv:
            self._generation += 1
            self._epoch += 1
            self._last_origin = None
            self._drop_pending_locked()
            if self._inflight is not None:
                self._inflight.dropped = True
            self._accepted_id = 0
            self._cache.clear()
            if self._owns_session and self._session is not None:
                old_session = self._session
                self._session = None
            self._cv.notify_all()
        if old_session is not None:
            old_session.close()

    def close(self) -> None:
        old_session = None
        thread: threading.Thread | None = None
        with self._cv:
            self._closed = True
            self._stopped = True
            self._generation += 1
            self._last_origin = None
            self._drop_pending_locked()
            if self._inflight is not None:
                self._inflight.dropped = True
            self._cache.clear()
            thread = self._thread
            if self._owns_session:
                old_session = self._session
                self._session = None
            self._cv.notify_all()
        if thread is not None and thread.is_alive():
            thread.join(timeout=0)
        if old_session is not None:
            old_session.close()

    def _prepare(self, text: str) -> tuple[str, dict[str, object] | None]:
        deterministic = self._deterministic(text)
        if not self._network_on():
            return deterministic, None
        span = ambiguous_pinyin_span(deterministic, self._terms, frequencies=self._frequencies)
        return deterministic, span

    def _deterministic(self, text: str) -> str:
        corrected, _changes = correct_hotwords(
            text,
            self._terms,
            max_ascii_edits=0,
            replacements=self._replacements,
        )
        return corrected

    def _network_on(self) -> bool:
        return bool(self._enabled and self._endpoint and self._timeout_s > 0 and not self._closed)

    def _ready_cached_locked(self, text: str) -> bool:
        cached = self._cache.get(text)
        return cached is not None and cached[0] == self._generation

    def _same_snapshot_locked(self, text: str) -> bool:
        # Repeated partials of the sentence currently being corrected.
        # cancel() clears this so the next utterance can be judged again.
        return self._last_origin == text

    def _drop_pending_locked(self) -> None:
        if self._pending is not None:
            self._pending.dropped = True
            self._pending = None

    def _job_matches_locked(self, text: str, generation: int, epoch: int) -> bool:
        for job in (self._pending, self._inflight):
            if job is None or job.dropped:
                continue
            if job.origin == text and job.generation == generation and job.epoch == epoch:
                return True
        return False

    def _drop_matching_locked(self, text: str, generation: int, epoch: int) -> None:
        for job in (self._pending, self._inflight):
            if job is None:
                continue
            if job.origin == text and job.generation == generation and job.epoch == epoch:
                job.dropped = True

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name="streaming-hotword", daemon=True)
        self._thread.start()

    def _ensure_session_locked(self) -> Any:
        if self._session is None:
            from .semif_judge import require_requests

            requests = require_requests()
            self._session = requests.Session()
            self._owns_session = True
        return self._session

    def _store_locked(self, origin: str, result: str) -> None:
        self._cache[origin] = (self._generation, result)
        self._cache.move_to_end(origin)
        while len(self._cache) > _CACHE_LIMIT:
            self._cache.popitem(last=False)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._stopped:
                    self._cv.wait()
                if self._pending is None and self._stopped:
                    return
                job = self._pending
                self._pending = None
                self._inflight = job
                assert job is not None
                session = self._ensure_session_locked()
                endpoint = self._endpoint
                timeout_s = self._timeout_s
                stale = job.dropped or job.generation != self._generation or job.epoch != self._epoch
            result = job.deterministic if stale else self._judge(job, session, endpoint, timeout_s)
            with self._cv:
                if (
                    not job.dropped
                    and job.generation == self._generation
                    and job.epoch == self._epoch
                    and job.req_id == self._accepted_id
                ):
                    self._store_locked(job.origin, result)
                if self._inflight is job:
                    self._inflight = None
                self._cv.notify_all()

    def _judge(
        self,
        job: _Job,
        session: Any,
        endpoint: str,
        timeout_s: float,
    ) -> str:
        span = job.span
        surface = str(span["surface"])
        start = int(cast(int, span["start"]))
        end = int(cast(int, span["end"]))
        words = {"keep": surface}
        options = {"keep": _criterion(job.deterministic, surface, None, start, end)}
        candidates = cast(list[object], span["candidates"])
        for index, candidate in enumerate(candidates):
            if len(options) >= _MAX_JUDGE_OPTIONS:
                break
            token = str(candidate)
            words[f"c{index}"] = token
            options[f"c{index}"] = _criterion(job.deterministic, surface, token, start, end)
        key = request_choice(
            session,
            endpoint,
            timeout_s,
            f"{job.deterministic}\n只判断这个跨度：「{surface}」",
            options,
            instructions=_INSTRUCTIONS,
        )
        if key is None or key == "keep" or key not in words:
            return job.deterministic
        replacement = words[key]
        if job.deterministic[start:end] != surface or replacement == surface:
            return job.deterministic
        return job.deterministic[:start] + replacement + job.deterministic[end:]
