"""Non-blocking hotword correction for one whole-sentence ASR hypothesis.

``submit`` returns deterministic edits immediately and is idempotent for the
same original text. ``finish`` accepts a final sentence that was never
submitted and waits at most one ``timeout_s``. Disabled mode does not start
a thread and does not call the network.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, cast

from .hotword_corrector import (
    _parse_replacement_pair,
    _protected_spans,
    ambiguous_pinyin_span,
    correct_hotwords,
)
from .jev_judge import request_jev_choices
from .runtime_config import (
    DEFAULT_JEV_TIMEOUT_S,
    DEFAULT_SEMIF_TIMEOUT_S,
    normalize_correction_provider,
    normalize_jev_timeout_s,
    normalize_semif_timeout_s,
)
from .semif_judge import request_choices

_CACHE_LIMIT = 32
_LEXICON_LIMIT = 64
_MAX_JUDGE_OPTIONS = 8
_INSTRUCTIONS = "这是整句识别假设。只能选择一个候选条件。不要改条件以外的字，不要删字。"

# Contextual alias (heard → word with a declared meaning) semantic-role judge.
# One bounded request can carry the pinyin pick plus at most 3 role picks.
_MAX_ALIAS_SPANS = 3
_CONTEXT_LIMIT = 256
_ROLE_CRITERIA = {
    "tool": "这里指的是软件工具、程序或插件。",
    "person": "这里指的是一个人。",
    "unclear": "不清楚，或者以上都不是。",
}
_ROLE_INSTRUCTIONS = (
    "判断句子中标记的「{surface}」在这个语境里指的是什么。"
    "只能从给定候选里选一个。拿不准就选“不清楚”。"
    "补充说明：用户的常用词里，{word} 是这个用户的{meaning}；"
    "语音识别经常把它误写成 {heard}。"
)
# Meta-linguistic mentions (“不是jev而是Jeff”, “不要把Jeff改成jev”) discuss
# the spelling itself; such spans are never judged or replaced.
_META_MENTION_MARKERS = ("而是", "改成", "写成", "说成", "叫作", "叫做")
_META_MENTION_WINDOW = 4
_NEGATION_MARKERS = ("不要", "不是", "没有", "不可以", "不会", "不能", "并未", "并不", "别", "不", "没")
_NEGATION_WINDOW = 8
# Affirmative software context. A lexicon meaning of "软件" is not evidence.
# The cue may sit in this utterance or the bounded previous-segment tail.
# Presence only makes a span eligible for the unchanged .70/.20 role gate.
_SOFTWARE_CONTEXT_CUES = (
    "打开", "安装", "启动", "运行", "执行", "卸载", "重启", "关闭",
    "更新", "升级", "配置", "部署", "编译", "调用",
    "软件", "工具", "程序", "插件", "应用", "浏览器", "终端", "命令",
    "脚本", "项目", "仓库", "测试", "调试", "代码", "进程",
)


def _criterion(sentence: str, surface: str, candidate: str | None, start: int, end: int) -> str:
    """Describe one allowed edit. The value is not spliced back into the text."""
    if candidate is None:
        return f"原句中的{surface}在此语境正确，保持原文。{sentence}"
    replaced = sentence[:start] + candidate + sentence[end:]
    return f"此语境应该使用{candidate}，应把{surface}替换为{candidate}，其余不变。{replaced}"


def _normalize_contextual_aliases(
    aliases: object,
) -> list[tuple[str, str, str]]:
    """Validate ``{'heard', 'word', 'meaning'}`` entries. Invalid ones drop out."""
    normalized: list[tuple[str, str, str]] = []
    if not isinstance(aliases, (list, tuple)):
        return normalized
    for entry in aliases:
        if not isinstance(entry, dict):
            continue
        heard = str(entry.get("heard", "")).strip()
        word = str(entry.get("word", "")).strip()
        meaning = str(entry.get("meaning", "")).strip()
        if not heard or not word or not meaning or heard == word:
            continue
        if len(heard) > 32 or len(word) > 32 or len(meaning) > 64:
            continue
        normalized.append((heard, word, meaning))
        if len(normalized) >= _LEXICON_LIMIT:
            break
    return normalized


def _meta_mention(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - _META_MENTION_WINDOW) : end + _META_MENTION_WINDOW]
    return any(marker in window for marker in _META_MENTION_MARKERS)


def _negated_before(text: str, start: int) -> bool:
    """A negation just before the span blocks judging.

    Hotword protection only covers the token glued to 不/不要. Spoken
    sentences such as 「不要打开jeff」 put the heard word a few characters
    later; those stay original too.
    """
    window = text[max(0, start - _NEGATION_WINDOW) : start]
    return any(marker in window for marker in _NEGATION_MARKERS)


def software_context_eligible(text: str, context: str = "") -> bool:
    """True when this utterance or the previous tail names a software action or object.

    Bare mentions stay ineligible even if the alias meaning says software.
    A cue does not replace the span; it only allows the role question.
    """
    blob = f"{context}\n{text}"
    return any(cue in blob for cue in _SOFTWARE_CONTEXT_CUES)


def _alias_spans(
    text: str,
    aliases: list[tuple[str, str, str]],
) -> list[dict[str, object]]:
    """Unprotected, non-meta occurrences of heard tokens, leftmost first.

    A heard token that occurs more than once in the same snapshot is skipped
    entirely: measured on the reference service, co-occurrence sentences
    drag every occurrence toward the tool role (person spans scored tool at
    0.76–0.98), and an all-or-nothing question abstains on everything.
    Single-occurrence role judging is the reliable, probed configuration.
    """
    if not aliases:
        return []
    protected = _protected_spans(text)
    grouped: list[list[tuple[int, int, str, str, str]]] = []
    for heard, word, meaning in aliases:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(heard)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        found: list[tuple[int, int, str, str, str]] = []
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if _overlaps_span(start, end, protected):
                continue
            if _meta_mention(text, start, end) or _negated_before(text, start):
                continue
            found.append((start, end, match.group(0), word, meaning))
        grouped.append(found)
    candidates = [item for found in grouped if len(found) == 1 for item in found]
    candidates.sort(key=lambda item: item[0])
    spans: list[dict[str, object]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, surface, word, meaning in candidates:
        if len(spans) >= _MAX_ALIAS_SPANS:
            break
        if _overlaps_span(start, end, occupied):
            continue
        occupied.append((start, end))
        spans.append(
            {
                "start": start,
                "end": end,
                "surface": surface,
                "heard_surface": surface,
                "word": word,
                "meaning": meaning,
            }
        )
    return spans


def _overlaps_span(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < stop and begin < end for begin, stop in spans)


class _Job:
    def __init__(
        self,
        req_id: int,
        generation: int,
        epoch: int,
        origin: str,
        deterministic: str,
        span: dict[str, object] | None,
        aliases: list[dict[str, object]],
        context: str,
    ) -> None:
        self.req_id = req_id
        self.generation = generation
        self.epoch = epoch
        self.origin = origin
        self.deterministic = deterministic
        self.span = span
        self.aliases = aliases
        self.context = context
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
        context: str = "",
        contextual_aliases: object = None,
        provider: str = "semif",
        jev_argv: Sequence[str] | None = None,
    ) -> None:
        self._terms, self._replacements, self._frequencies = _compile_hotwords(hotwords)
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._enabled = enabled
        self._provider = normalize_correction_provider(provider)
        self._jev_argv = tuple(jev_argv) if jev_argv else None
        self._owns_session = session is None
        self._session = session
        self._proc: Any = None
        # Previous-segment tail, bounded. Judgment-only input: never edited,
        # never returned, never committed.
        self._context = str(context)[-_CONTEXT_LIMIT:]
        self._aliases = _normalize_contextual_aliases(contextual_aliases)
        self._cv = threading.Condition()
        self._pending: _Job | None = None
        self._inflight: _Job | None = None
        self._accepted_id = 0
        self._next_id = 0
        self._generation = 0
        self._epoch = 0
        self._last_origin: str | None = None
        self._cache: OrderedDict[str, tuple[int, str, str]] = OrderedDict()
        self._closed = False
        self._stopped = False
        self._thread: threading.Thread | None = None

    def submit(self, text: str) -> str:
        """Return the deterministic snapshot.

        The same original text does not cancel an in-flight judgment or drop
        a ready result. A different text replaces the single pending request.
        """
        deterministic, span, alias_spans = self._prepare(text)
        with self._cv:
            if self._closed:
                return deterministic
            if self._ready_cached_locked(text) or self._same_snapshot_locked(text):
                return deterministic
            needs_judge = span is not None or bool(alias_spans)
            if needs_judge and self._provider != "jev":
                from .semif_judge import require_requests

                require_requests()
            self._epoch += 1
            self._last_origin = text
            self._drop_pending_locked()
            if self._inflight is not None:
                self._inflight.dropped = True
                self._kill_proc_locked()
            if not needs_judge:
                self._cv.notify_all()
                return deterministic
            self._next_id += 1
            job = _Job(
                self._next_id,
                self._generation,
                self._epoch,
                text,
                deterministic,
                span,
                alias_spans,
                self._context,
            )
            self._pending = job
            self._accepted_id = job.req_id
            self._ensure_thread_locked()
            self._cv.notify()
        return deterministic

    def poll(self, text: str) -> str:
        """Return a ready correction for this original snapshot, without waiting."""
        with self._cv:
            cached = self._cache.get(text)
            if cached is not None and cached[0] == self._generation and cached[1] == self._context:
                return cached[2]
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
                if cached is not None and cached[0] == generation and cached[1] == self._context:
                    return cached[2]
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
            self._kill_proc_locked()
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
            self._kill_proc_locked()
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

    def _prepare(self, text: str) -> tuple[str, dict[str, object] | None, list[dict[str, object]]]:
        deterministic = self._deterministic(text)
        if not self._network_on():
            return deterministic, None, []
        span = ambiguous_pinyin_span(deterministic, self._terms, frequencies=self._frequencies)
        alias_spans = _alias_spans(deterministic, self._aliases)
        if alias_spans and not software_context_eligible(deterministic, self._context):
            alias_spans = []
        return deterministic, span, alias_spans

    def _deterministic(self, text: str) -> str:
        corrected, _changes = correct_hotwords(
            text,
            self._terms,
            max_ascii_edits=0,
            replacements=self._replacements,
        )
        return corrected

    def _network_on(self) -> bool:
        if not self._enabled or self._timeout_s <= 0 or self._closed:
            return False
        # Official Jev is only for declared aliases. Disabled mode and an
        # empty alias list never start the CLI. SemIf still needs an endpoint.
        if self._provider == "jev":
            return bool(self._aliases)
        return bool(self._endpoint)

    def _ready_cached_locked(self, text: str) -> bool:
        cached = self._cache.get(text)
        return cached is not None and cached[0] == self._generation and cached[1] == self._context

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
        self._kill_proc_locked()

    def _kill_proc_locked(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
        except OSError:
            return

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

    def _store_locked(self, origin: str, context: str, result: str) -> None:
        self._cache[origin] = (self._generation, context, result)
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
                session = None if self._provider == "jev" else self._ensure_session_locked()
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
                    self._store_locked(job.origin, job.context, result)
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
        deterministic = job.deterministic
        updated = deterministic
        deadline = time.monotonic() + max(0.0, timeout_s)

        def ask(state: str, questions: dict[str, dict[str, Any]]) -> dict[str, str | None]:
            if job.dropped or time.monotonic() >= deadline:
                return {}
            if self._provider == "jev":
                remaining = deadline - time.monotonic()
                if remaining < 0.15:
                    return {}
                return request_jev_choices(
                    remaining,
                    state,
                    questions,
                    argv=self._jev_argv,
                    on_process=lambda proc: self._register_proc(proc, job),
                )
            return request_choices(session, endpoint, timeout_s, state, questions)

        span = job.span
        if span is not None:
            surface = str(span["surface"])
            start = int(cast(int, span["start"]))
            end = int(cast(int, span["end"]))
            words = {"keep": surface}
            options = {"keep": _criterion(deterministic, surface, None, start, end)}
            candidates = cast(list[object], span["candidates"])
            for index, candidate in enumerate(candidates):
                if len(options) >= _MAX_JUDGE_OPTIONS:
                    break
                token = str(candidate)
                words[f"c{index}"] = token
                options[f"c{index}"] = _criterion(deterministic, surface, token, start, end)
            results = ask(
                self._state(job, surface),
                {"pick": {"type": "choice", "instructions": _INSTRUCTIONS, "criteria": options}},
            )
            picked = results.get("pick")
            if picked is not None and picked != "keep" and picked in words:
                replacement = words[picked]
                if updated[start:end] == surface and replacement != surface:
                    updated = updated[:start] + replacement + updated[end:]

        # Semantic-role judgments: one request per span. Batching several
        # spans into one request measurably degraded per-span accuracy on the
        # reference service, so a job makes at most _MAX_ALIAS_SPANS bounded
        # sequential requests instead.
        alias_edits: list[tuple[int, int, str, str]] = []
        for item in job.aliases[:_MAX_ALIAS_SPANS]:
            surface = str(item["surface"])
            results = ask(
                self._state(job, surface),
                {
                    "role": {
                        "type": "choice",
                        "instructions": _ROLE_INSTRUCTIONS.format(
                            surface=surface,
                            word=str(item["word"]),
                            meaning=str(item["meaning"]),
                            heard=surface,
                        ),
                        "criteria": dict(_ROLE_CRITERIA),
                    }
                },
            )
            if results.get("role") != "tool":
                continue
            start = int(cast(int, item["start"]))
            end = int(cast(int, item["end"]))
            word = str(item["word"])
            if deterministic[start:end] == surface and word != surface:
                alias_edits.append((start, end, surface, word))
        # Offsets refer to the deterministic snapshot; apply right to left and
        # skip a span whose text shifted under an accepted pinyin replacement.
        for start, end, surface, word in sorted(alias_edits, reverse=True):
            if updated[start:end] != surface:
                continue
            updated = updated[:start] + word + updated[end:]
        return updated

    def _register_proc(self, proc: Any, job: _Job) -> None:
        with self._cv:
            self._proc = proc
            if job.dropped or self._closed:
                self._kill_proc_locked()

    @staticmethod
    def _state(job: _Job, surface: str) -> str:
        lines = []
        if job.context:
            lines.append(f"上一段：{job.context}")
        lines.append(job.deterministic)
        lines.append(f"只判断这个跨度：「{surface}」")
        return "\n".join(lines)


def corrector_from_args(
    args: object,
    hotwords: list[str],
    *,
    context: str = "",
) -> StreamingHotwordCorrector:
    """Build a corrector with the standard SemIf wiring from CLI/config args.

    ``context`` is the bounded previous-segment tail used only as judgment
    input. Callers that own a session or lifecycle can still construct
    ``StreamingHotwordCorrector`` directly.
    """
    provider = normalize_correction_provider(getattr(args, "correction_provider", "semif"))
    if provider == "jev":
        timeout_s = normalize_jev_timeout_s(getattr(args, "jev_timeout_s", DEFAULT_JEV_TIMEOUT_S))
    else:
        timeout_s = normalize_semif_timeout_s(getattr(args, "semif_timeout_s", DEFAULT_SEMIF_TIMEOUT_S))
    return StreamingHotwordCorrector(
        list(hotwords),
        endpoint=str(getattr(args, "semif_endpoint", "") or ""),
        timeout_s=timeout_s,
        enabled=bool(getattr(args, "enable_semif_correction", False)),
        context=context,
        contextual_aliases=getattr(args, "contextual_aliases", None),
        provider=provider,
    )
