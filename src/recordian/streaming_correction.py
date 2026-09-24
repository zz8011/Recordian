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
# One bounded request can carry the pinyin pick plus at most 3 distinct role
# questions; spans that repeat the exact same clause text share one question.
_MAX_ALIAS_SPANS = 3
_CONTEXT_LIMIT = 256
# Punctuation that ends a clause. Co-occurrence protection and the role
# question are scoped to one clause, so a repeated mention in a *different*
# clause cannot drag a person name toward the tool role.
_CLAUSE_BOUNDARY_RE = re.compile(r"[，。！？；：、,.!?;:\n]+")
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
# A bare alias in a person role is a person/software ambiguity even when the
# clause names software around it: 「Jeff帮我调试脚本」 and 「我和Jeff讨论模型」
# say what the person does or talks about, not that the alias names the tool.
# Measured on the reference service, those mentions were answered "tool" at
# 0.60-0.74 and the person spelling was rewritten. No role question is asked
# for such an occurrence, so the verdict cannot be talked into the tool
# meaning. A software/model/tool noun attached directly to the alias
# (「jeff工具」, 「jeff的模型」) removes the ambiguity: that occurrence stays
# judgeable. The guard is structural — case is irrelevant.
_HELP_ROLE_RE = re.compile(r"^(?:帮|替)(?:我|你|您|他|她|它|咱|们|忙|个|一下)")
_COORDINATED_BEFORE_RE = re.compile(r"(?:我|你|您|他|她|咱|们)[和跟与同]$")
_COORDINATED_AFTER_RE = re.compile(r"^(?:和|跟|与|同)(?:我|你|您|他|她|咱|们)")
_ARTIFACT_NOUNS = (
    "软件", "工具", "程序", "插件", "应用", "浏览器", "终端", "命令",
    "脚本", "项目", "仓库", "代码", "进程", "模型", "服务器", "服务",
    "系统", "平台", "框架", "引擎", "客户端", "库",
)
# Affirmative software context. A lexicon meaning of "软件" is not evidence.
# The cue may sit in this utterance or the bounded previous-segment tail.
# Presence only makes a span eligible for the unchanged .70/.20 role gate.
_SOFTWARE_CONTEXT_CUES = (
    "打开", "安装", "启动", "运行", "执行", "卸载", "重启", "关闭",
    "更新", "升级", "配置", "部署", "编译", "调用",
    "软件", "工具", "程序", "插件", "应用", "浏览器", "终端", "命令",
    "脚本", "项目", "仓库", "测试", "调试", "代码", "进程",
    # Technical context that names the software without a verb cue
    # (e.g. “服务器上的jeff模型怎么样”, “jeff的推理延迟很低”).
    "模型", "服务器", "推理",
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


# Spaces an IME or ASR may insert beside an ASCII name. The guard looks
# through them; the original offsets and characters stay unchanged.
_ALIAS_GAP = " \t\u3000\u00a0"


def _artifact_noun_attached(tail: str) -> bool:
    """True when a software/model/tool noun directly modifies the alias.

    A single gap (「Jeff 工具」, 「Jeff 的 模型」) is still the same attachment.
    """
    tail = tail.lstrip(_ALIAS_GAP)
    if tail.startswith("的"):
        tail = tail[1:].lstrip(_ALIAS_GAP)
    return tail.startswith(_ARTIFACT_NOUNS)


def _bare_person_alias(text: str, start: int, end: int) -> bool:
    """True when this occurrence is a person mention, not an artifact name.

    Two structural person roles count: the subject of a helping predicate
    (「jeff帮我调试脚本」, 「jeff替我看看」) and a coordinated participant
    (「我和jeff讨论模型」, 「jeff和我聊脚本」). Whitespace glued to the alias
    does not hide those roles (「Jeff 帮我调试脚本」, 「我和 Jeff 讨论模型」).
    The clause's software words (调试/脚本/模型) describe what the person does
    or talks about; they are not evidence that the bare alias names the tool.
    An attached artifact noun (「jeff工具」, 「jeff 的模型」) keeps the
    occurrence judgeable.
    """
    tail = text[end:].lstrip(_ALIAS_GAP)
    if _artifact_noun_attached(text[end:]):
        return False
    if _HELP_ROLE_RE.match(tail) or _COORDINATED_AFTER_RE.match(tail):
        return True
    prefix = text[:start].rstrip(_ALIAS_GAP)
    return bool(_COORDINATED_BEFORE_RE.search(prefix))


def software_context_eligible(text: str, context: str = "") -> bool:
    """True when this utterance or the previous tail names a software action or object.

    Bare mentions stay ineligible even if the alias meaning says software.
    A cue does not replace the span; it only allows the role question.
    """
    blob = f"{context}\n{text}"
    return any(cue in blob for cue in _SOFTWARE_CONTEXT_CUES)


def _clause_spans(text: str) -> list[tuple[int, int]]:
    """Punctuation-delimited clause ranges, boundaries excluded."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def _clause_index(clauses: list[tuple[int, int]], position: int) -> int:
    for index, (begin, stop) in enumerate(clauses):
        if begin <= position < stop:
            return index
    return max(0, len(clauses) - 1)


def _alias_spans(
    text: str,
    aliases: list[tuple[str, str, str]],
    context: str = "",
) -> list[dict[str, object]]:
    """Unprotected, non-meta occurrences of heard tokens, leftmost first.

    An alias is judged per punctuation-delimited clause, and only when every
    occurrence of that heard token in this snapshot can be explained by its
    own clause: the clause holds the token exactly once and carries a software
    cue (in the clause, or in the bounded previous-segment tail). Then the
    role question is asked on that clause text alone, so a repeated software
    mention in a different explicit clause is still corrected and no other
    clause pollutes the verdict.

    One occurrence that repeats inside a single clause, or that sits in a
    clause with no software cue of its own, vetoes this heard token for the
    whole snapshot. Measured on the reference service, those mixed sentences
    drag person spans toward the tool role (0.76-0.98), so every occurrence
    keeps the original text; the veto is per heard token, not per snapshot.
    An occurrence that ``_bare_person_alias`` explains as a person is skipped
    individually — it is neither judged nor allowed to veto its neighbours —
    so 「打开jeff工具检查这个项目，Jeff帮我调试脚本」 still corrects the
    explicit tool mention while the bare helper name stays untouched.
    """
    if not aliases:
        return []
    protected = _protected_spans(text)
    clauses = _clause_spans(text)
    candidates: list[tuple[int, int, str, str, str, str]] = []
    for heard, word, meaning in aliases:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(heard)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        per_clause: dict[int, list[tuple[int, int, str]]] = {}
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if _overlaps_span(start, end, protected):
                continue
            if (
                _meta_mention(text, start, end)
                or _negated_before(text, start)
                or _bare_person_alias(text, start, end)
            ):
                continue
            index = _clause_index(clauses, start)
            per_clause.setdefault(index, []).append((start, end, match.group(0)))
        eligible: list[tuple[int, int, str, str]] = []
        for index in sorted(per_clause):
            found = per_clause[index]
            begin, stop = clauses[index]
            focus = text[begin:stop]
            if len(found) != 1 or not software_context_eligible(focus, context):
                # A repeat inside one clause, or a mention whose own clause
                # names no software action or object, vetoes this alias for
                # the whole snapshot. That is the measured mixed-sentence
                # protection: nothing is judged, nothing is replaced.
                eligible = []
                break
            start, end, surface = found[0]
            eligible.append((start, end, surface, focus))
        for start, end, surface, focus in eligible:
            candidates.append((start, end, surface, word, meaning, focus))
    candidates.sort(key=lambda item: item[0])
    spans: list[dict[str, object]] = []
    occupied: list[tuple[int, int]] = []
    # The cap bounds distinct role questions, not spans: a clause text that
    # was already admitted shares its one verdict with every twin span.
    judged: set[tuple[str, str, str, str]] = set()
    for start, end, surface, word, meaning, focus in candidates:
        key = (surface, word, meaning, focus)
        if key not in judged:
            if len(judged) >= _MAX_ALIAS_SPANS:
                continue
            judged.add(key)
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
                "focus": focus,
            }
        )
    return spans


def _overlaps_span(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < stop and begin < end for begin, stop in spans)


def _apply_alias_edits(base: str, edits: list[tuple[int, int, str, str]]) -> str:
    """Apply role replacements right to left; offsets refer to the snapshot."""
    updated = base
    for start, end, surface, word in sorted(edits, reverse=True):
        if updated[start:end] != surface:
            continue
        updated = updated[:start] + word + updated[end:]
    return updated


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
        # Last decided, safe text of this job. Read only by a bounded
        # ``finish`` that ran out of budget.
        self.partial: str | None = None


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
        another request. Out of budget, the verdicts already decided are
        returned and kept for this exact snapshot, so repeating ``finish`` or
        ``poll`` on the same text answers the same text instead of falling
        back to the raw one. Disabled mode returns the deterministic text.
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
                    # Out of budget: keep the verdicts already decided inside
                    # it, drop the rest, and never delay the caller further.
                    partial = self._cache_partial_locked(text, generation, epoch)
                    self._drop_matching_locked(text, generation, epoch)
                    return partial if partial is not None else deterministic
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
        # The software-context gate is applied per clause inside _alias_spans.
        alias_spans = _alias_spans(deterministic, self._aliases, self._context)
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

    def _cache_partial_locked(self, text: str, generation: int, epoch: int) -> str | None:
        """Keep this exact job's decided part as the answer for its snapshot.

        Stored under the same origin/generation/context key as a final
        result, so a repeated ``finish``/``poll`` of the same snapshot
        returns the same text instead of falling back to the untouched
        original. ``finish`` drops this job on the next line, and only an
        accepted, undropped, same-epoch job can store a late reply, so the
        kept part cannot be overwritten afterwards. ``cancel``/``close``
        clear the cache.
        """
        for job in (self._pending, self._inflight):
            if job is None or job.dropped:
                continue
            if job.origin == text and job.generation == generation and job.epoch == epoch:
                if job.partial is not None:
                    self._store_locked(job.origin, job.context, job.partial)
                return job.partial
        return None

    def _publish_partial(self, job: _Job, text: str) -> None:
        """Record the safe edits decided so far for this exact job.

        A verdict that already cleared the unchanged role gate may be kept
        when the single job budget expires before the remaining clauses
        answered. A dropped, stale, or closed job publishes nothing, so a
        late reply can never land in another sentence.
        """
        with self._cv:
            if job.dropped or job.generation != self._generation or job.epoch != self._epoch:
                return
            job.partial = text

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
                    self._publish_partial(job, updated)

        # Semantic-role judgments: one request per distinct clause, at most
        # _MAX_ALIAS_SPANS distinct clause questions. Batching several spans
        # into one request measurably degraded per-span accuracy on the
        # reference service, but clauses with the exact same text share one
        # verdict inside this job, so a repeated sentence stays inside one
        # bounded budget.
        alias_edits: list[tuple[int, int, str, str]] = []
        verdicts: dict[tuple[str, str, str, str], str | None] = {}
        for item in job.aliases:
            surface = str(item["surface"])
            word = str(item["word"])
            focus = str(item.get("focus") or job.deterministic)
            key = (surface, word, str(item["meaning"]), focus)
            if key not in verdicts:
                results = ask(
                    self._state(job, surface, focus),
                    {
                        "role": {
                            "type": "choice",
                            "instructions": _ROLE_INSTRUCTIONS.format(
                                surface=surface,
                                word=word,
                                meaning=str(item["meaning"]),
                                heard=surface,
                            ),
                            "criteria": dict(_ROLE_CRITERIA),
                        }
                    },
                )
                verdicts[key] = results.get("role")
            if verdicts[key] != "tool":
                continue
            start = int(cast(int, item["start"]))
            end = int(cast(int, item["end"]))
            if deterministic[start:end] == surface and word != surface:
                alias_edits.append((start, end, surface, word))
                self._publish_partial(job, _apply_alias_edits(updated, alias_edits))
        # Offsets refer to the deterministic snapshot; apply right to left and
        # skip a span whose text shifted under an accepted pinyin replacement.
        return _apply_alias_edits(updated, alias_edits)

    def _register_proc(self, proc: Any, job: _Job) -> None:
        with self._cv:
            self._proc = proc
            if job.dropped or self._closed:
                self._kill_proc_locked()

    @staticmethod
    def _state(job: _Job, surface: str, focus: str | None = None) -> str:
        lines = []
        if job.context:
            lines.append(f"上一段：{job.context}")
        lines.append(job.deterministic if focus is None else focus)
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
