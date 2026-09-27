"""Realtime ASR worker components.

Provides the incremental commit accumulator and the worker thread that
drives a streaming/realtime ASR session, feeding partial results to the
accumulator and producing the final transcription.
"""
from __future__ import annotations

import argparse
import array
import inspect
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import streaming_correction as _streaming_correction

# NOTE: These imports will be replaced by state_manager imports once that
#       module is available.  For now they are passed in as parameters.
from .duration_guard import (
    CONTINUOUS_CONTEXT_CHARS,
    CONTINUOUS_HELD_TAIL_CHARS,
)
from .hotword_corrector import lexicon_from_args
from .linux_commit import (
    CompositionRefusedError,
    paste_to_enter_delay_seconds,
    resolve_streaming_committer,
    send_backspaces,
    send_hard_enter,
)
from .linux_dictate import open_monitor_stream_reader
from .providers import provider_supports_realtime
from .spoken_formatting import NUMBER_MARKERS

# ---------------------------------------------------------------------------
# Handle returned by the worker starter
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _RealtimeASRWorkerHandle:
    """Opaque handle returned when a realtime ASR worker is started."""

    thread: threading.Thread
    final_text: str = ""
    detected_language: str = ""
    transcribe_latency_ms: float = 0.0
    commit_info: dict[str, object] | None = None
    error: str = ""
    cancel_session: Callable[[], None] | None = None
    # Set by the controller (timeout / cancel path). Once set, the worker
    # stops writing and never commits its partial text.
    cancel_event: threading.Event | None = None
    # Last preview hypothesis, for diagnostics only. Never committed as final.
    partial_text: str = ""
    # True when the composition session went stale (focus lost / user typed /
    # context destroyed). The controller must suppress fallback commits.
    session_stale: bool = False
    # True while an fcitx composition session owns a preedit on the focused
    # context. Lets the controller distinguish an uncertain deadline (preedit
    # may have been left/committed by the toolkit — must not fall back to a
    # full-sentence commit) from preview-only streaming (nothing written yet,
    # single final commit is safe).
    composition_active: bool = False
    # Irreversible: True from the moment a composition session was bound to
    # the focused input context until process end. Unlike
    # ``composition_active`` it is never cleared by the worker's ``finally``,
    # so a controller reading it after a deadline cancel cannot mistake a
    # mid-teardown session for preview-only streaming.
    composition_started: bool = False
    # Structured terminal state; one of "committed" / "released_for_refine" /
    # "stale" / "uncertain" / "cancelled" / "no_composition". Mirrors the
    # ``outcome`` key inside ``commit_info``.
    outcome: str = ""
    # The live composition session when ``outcome == "released_for_refine"``:
    # the postprocess pipeline must commit the refined text through this
    # exact token (same input context), never re-Begin a new session.
    composition_session: Any = None
    # True when the streaming corrector already applied its end-of-sentence
    # judgment to ``final_text`` (the pipeline must not judge it twice).
    semif_applied: bool = False
    # True when a composition-capable backend REFUSED to start a session
    # (user preedit / no focus / sensitive / table full / lost reply).
    # Distinct from "no composition support": the utterance is suppressed —
    # zero commits, zero backspaces, no fallback of any kind. The ASR still
    # runs so the transcript stays available for manual copy.
    composition_refused: bool = False
    # Successful CommitSegment calls on the original token. Postprocess must
    # not refine or re-commit the whole file over this prefix.
    segments_committed: int = 0
    continuous: bool = False
    transcript_path: str = ""


# Glue tails stay raw across a segment cut (no space when joined). An ASCII
# word tail is also held so the next English word can take one space; it is
# not run through the formatter by itself.
#
# Spoken digits include 幺 (the "1" used in ports / IDs / phone numbers):
# cutting before it would commit a lone "幺" and leave the next segment's
# digits unreadable as one number.
_CN_NUMERAL = "零〇一二两三四五六七八九十百千万亿点．.幺"
# Explicit number markers are held together with the digits that follow them
# (百分之 三 -> 3%, 号码 三 -> 号码3). Committing the marker alone is wrong
# twice over: the next segment restarts the number, and a bare "三四" would
# look like an approximation (概数) instead of the digits 34. The marker list
# is the formatter's NUMBER_MARKERS so the two number paths cannot drift.
_CN_NUMBER_MARKER = "|".join(sorted(NUMBER_MARKERS, key=len, reverse=True))
_HELD_ASCII_DIGITS = r"[0-9][0-9.,:/%+-]*"
_GLUE_TAIL_RE = re.compile(
    r"(?:https?://\S+"
    r"|www(?:点|\.)[0-9A-Za-z点.\-]*"
    r"|[0-9A-Za-z]+(?:点[0-9A-Za-z.\-]+)+"
    r"|\d{1,3}(?:\.\d{1,3}){2,3}"
    r"|\d[\d.,:/%+-]*"
    rf"|百分之(?:{_HELD_ASCII_DIGITS}|[{_CN_NUMERAL}]*)"
    rf"|(?:{_CN_NUMBER_MARKER})(?:是|为|[:：])?\s*"
    rf"(?:{_HELD_ASCII_DIGITS}|[{_CN_NUMERAL}]+)"
    rf"|[{_CN_NUMERAL}]+"
    r")$"
)
_WORD_TAIL_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?$")


def _split_held_tail(text: str, *, limit: int = CONTINUOUS_HELD_TAIL_CHARS) -> tuple[str, str]:
    """Split a raw trailing number, URL, Chinese numeral, or English word.

    The caller formats only the returned prefix. The tail stays raw and is
    joined to the next ASR hypothesis before a new formatter run.
    """
    if not text:
        return "", ""
    glue = _GLUE_TAIL_RE.search(text)
    word = _WORD_TAIL_RE.search(text)
    match: re.Match[str] | None
    if glue is not None and word is not None:
        match = glue if len(glue.group(0)) >= len(word.group(0)) else word
    else:
        match = glue or word
    if match is None:
        return text, ""
    held = match.group(0)
    if not held:
        return text, ""
    if len(held) <= limit:
        return text[: match.start()], held
    cut = match.start() + (len(held) - limit)
    return text[:cut], held[-limit:]


def _pcm_rms(raw: bytes) -> float:
    usable = len(raw) - (len(raw) % 4)
    if usable <= 0:
        return 0.0
    samples = array.array("f")
    samples.frombytes(raw[:usable])
    count = len(samples)
    if count <= 0:
        return 0.0
    total = 0.0
    for value in samples:
        total += float(value) * float(value)
    return math.sqrt(total / count)


# ---------------------------------------------------------------------------
# Internal helper – commit text directly (used as fallback)
# ---------------------------------------------------------------------------

def _commit_text(
    committer: Any,
    text: str,
    *,
    auto_hard_enter: bool = False,
) -> dict[str, object]:
    """Commit *text* through *committer* with optional auto hard-enter.

    Parameters
    ----------
    committer:
        An object exposing ``.commit(text)`` and ``.backend_name``.
    text:
        The text to commit.
    auto_hard_enter:
        If ``True`` and the commit succeeded, send a hard Enter key after
        a provider-specific delay.

    Returns
    -------
    dict[str, object]
        A summary dict with ``backend``, ``committed``, and ``detail`` keys.
    """
    stripped = text.strip()
    if not stripped:
        return {"backend": committer.backend_name, "committed": False, "detail": "empty_text"}
    try:
        result = committer.commit(stripped)
        detail = str(result.detail)
        if result.committed and auto_hard_enter:
            enter_delay_s = paste_to_enter_delay_seconds(result)
            if enter_delay_s > 0.0:
                time.sleep(enter_delay_s)
            enter_result = send_hard_enter(committer)
            enter_detail = str(enter_result.detail)
            detail = f"{detail};{enter_detail}" if detail else enter_detail
        return {"backend": result.backend, "committed": result.committed, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"backend": committer.backend_name, "committed": False, "detail": str(exc)}


# ---------------------------------------------------------------------------
# Incremental commit accumulator
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _RealtimeCommitAccumulator:
    """Accumulates realtime ASR partial results and commits them
    incrementally according to a flush policy that depends on the
    committer backend.

    Attributes
    ----------
    committer:
        The streaming committer object.
    committed_text:
        Text that has already been flushed/committed.
    chunk_count:
        Number of committed chunks so far.
    any_committed:
        Whether at least one chunk was successfully committed.
    last_backend:
        Name of the backend used for the most recent commit.
    last_result:
        The most recent commit result object.
    error:
        Error message if a commit failed.
    pending_text:
        Buffered text waiting to be flushed.
    last_flush_started_at:
        Monotonic timestamp when the current pending buffer started
        accumulating (used for time-based flush).
    """

    committer: Any
    committed_text: str = ""
    chunk_count: int = 0
    any_committed: bool = False
    last_backend: str = ""
    last_result: Any | None = None
    error: str = ""
    pending_text: str = ""
    last_flush_started_at: float = 0.0
    previous_hypothesis: str = ""
    hotwords: list[str] | None = None
    raw_text: str = ""

    # -- flush policy -------------------------------------------------------

    def _flush_policy(self) -> tuple[int, float]:
        """Return ``(min_chars, max_delay_seconds)`` for the current backend.

        ``xdotool-clipboard`` buffers more aggressively (12 chars / 0.35 s)
        to avoid clipboard contention; all other backends flush every
        single character immediately.
        """
        backend = str(getattr(self.committer, "backend_name", "")).strip().lower()
        if backend == "xdotool-clipboard":
            return 12, 0.35
        return 1, 0.0

    # -- flush execution ----------------------------------------------------

    def _flush_pending(self) -> None:
        """Flush buffered pending text through the committer."""
        if not self.pending_text or self.error:
            return
        token = self.pending_text
        self.pending_text = ""
        try:
            result = self.committer.commit(token)
        except Exception:  # noqa: BLE001
            # A single missed focus must not silence the rest of the utterance.
            self.pending_text = token
            self.error = ""
            return
        self.committed_text += token
        self.chunk_count += 1
        self.last_result = result
        self.last_backend = str(
            getattr(result, "backend", "") or getattr(self.committer, "backend_name", "unknown")
        )
        if bool(getattr(result, "committed", False)):
            self.any_committed = True

    # -- public API ---------------------------------------------------------

    def append_text(self, text: str) -> None:
        """Buffer *text* and flush when the policy threshold is met."""
        token = str(text)
        if not token or self.error:
            return
        self.pending_text += token
        if self.last_flush_started_at <= 0.0:
            self.last_flush_started_at = time.monotonic()
        min_chars, max_delay_s = self._flush_policy()
        now = time.monotonic()
        if len(self.pending_text) >= min_chars or (
            max_delay_s > 0.0 and now - self.last_flush_started_at >= max_delay_s
        ):
            self._flush_pending()

    def sync_to(self, hypothesis: str) -> None:
        """Make the input match the ASR stream text, with no extra rewriting."""
        if self.error:
            return
        self._flush_pending()
        current = str(hypothesis)
        if not current or current == self.committed_text:
            return
        if current.startswith(self.committed_text):
            self.append_text(current[len(self.committed_text):])
            self._flush_pending()
            return
        prefix_len = 0
        limit = min(len(self.committed_text), len(current))
        while prefix_len < limit and self.committed_text[prefix_len] == current[prefix_len]:
            prefix_len += 1
        if prefix_len < 2:
            return
        delete_n = len(self.committed_text) - prefix_len
        if delete_n <= 0:
            return
        result = send_backspaces(self.committer, delete_n)
        if not bool(getattr(result, "committed", False)):
            return
        self.committed_text = self.committed_text[:prefix_len]
        extra = current[prefix_len:]
        if extra:
            self.append_text(extra)
            self._flush_pending()

    def finalize(self, *, final_text: str, auto_hard_enter: bool) -> dict[str, object]:
        """Flush remaining buffered text and optionally send a hard Enter.

        Parameters
        ----------
        final_text:
            The complete final transcription from the ASR session.
        auto_hard_enter:
            Whether to press Enter after the final commit.

        Returns
        -------
        dict[str, object]
            Summary with ``backend``, ``committed``, and ``detail`` keys.
        """
        self._flush_pending()
        if final_text and final_text != self.committed_text and not self.error:
            self.sync_to(final_text)
        elif self.error and not self.any_committed and final_text.strip():
            return _commit_text(self.committer, final_text, auto_hard_enter=auto_hard_enter)

        backend = self.last_backend or getattr(self.committer, "backend_name", "unknown")
        details: list[str] = []
        if self.chunk_count:
            details.append(f"realtime_chunks:{self.chunk_count}")
        if self.error:
            details.append(f"realtime_error:{self.error}")
        if auto_hard_enter and self.any_committed and self.last_result is not None:
            enter_delay_s = paste_to_enter_delay_seconds(self.last_result)
            if enter_delay_s > 0.0:
                time.sleep(enter_delay_s)
            enter_result = send_hard_enter(self.committer)
            enter_detail = str(getattr(enter_result, "detail", "")).strip()
            if enter_detail:
                details.append(enter_detail)
        detail = ";".join(part for part in details if part) or "realtime_complete"
        return {
            "backend": backend,
            "committed": self.any_committed,
            "detail": detail,
            "committed_text": self.committed_text,
        }


# ---------------------------------------------------------------------------
# Worker starter
# ---------------------------------------------------------------------------

def _start_realtime_asr_worker(
    *,
    args: argparse.Namespace,
    provider: Any,
    record_handle: Any,
    committer: Any,
    enable_local_commit: bool,
    auto_hard_enter: bool,
    resolve_hotwords: Callable[[], list[str]],
    normalize_final_text: Callable[[str], str],
    on_state: Callable[[dict[str, object]], None],
    refine_enabled: bool = False,
    on_capture_fatal: Callable[[str], None] | None = None,
) -> _RealtimeASRWorkerHandle | None:
    """Start a background thread that drives a realtime ASR session.

    The worker streams partial hypotheses into a *composition session*
    (preedit) bound to the focused input context and commits the final text
    exactly once, after deterministic hotword correction. When no
    composition backend is available, streaming is preview-only
    (``realtime_asr_partial`` state events) and the final commit is left to
    the postprocess pipeline — the worker never emits per-keystroke
    synthetic typing and never rewrites already-committed text.

    Contract:
    - ``worker.cancel_event`` set (stop/cancel/timeout) → the worker stops
      writing, cancels the composition session, and never commits.
    - provider failure → composition cancelled, partial text kept only in
      ``worker.partial_text`` (never silently committed as final).
    - ``refine_enabled`` → the worker releases the preedit without
      committing; the pipeline refines and commits once.
    - ``worker.session_stale`` → focus was lost or the user typed; the
      controller must suppress fallback commits to avoid writing into the
      wrong context (or duplicating a toolkit-committed preedit).
    """
    if not bool(getattr(args, "enable_streaming_commit", False)):
        return None
    if not provider_supports_realtime(provider):
        return None

    reader = open_monitor_stream_reader(record_handle)
    if reader is None:
        return None

    chunk_size_sec = float(getattr(provider, "realtime_chunk_size_sec", 0.5) or 0.5)
    sample_rate = int(
        getattr(record_handle, "monitor_sample_rate", getattr(args, "sample_rate", 16000)) or 16000
    )
    channels = max(1, int(getattr(record_handle, "monitor_channels", getattr(args, "channels", 1)) or 1))
    chunk_bytes = max(4, int(sample_rate * chunk_size_sec) * channels * 4)

    worker = _RealtimeASRWorkerHandle(thread=threading.Thread(target=lambda: None))
    cancel_event = threading.Event()
    worker.cancel_event = cancel_event

    def _cancel_asr_session(session: Any) -> None:
        cancel_event.set()
        cancel = getattr(session, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass

    def _run() -> None:
        from .linux_commit import open_composition_session

        session = None
        asr_session = None
        corrector = None
        streaming_committer = resolve_streaming_committer(committer)

        def _fatal(reason: str) -> None:
            if cancel_event.is_set() or not callable(on_capture_fatal):
                return
            try:
                on_capture_fatal(str(reason))
            except Exception:  # noqa: BLE001
                pass

        try:
            if enable_local_commit and not cancel_event.is_set():
                # The cancel check gates Begin itself: a controller deadline
                # that fired while the Begin call was still pending must not
                # let a NEW session appear afterwards — nobody could retract
                # its preedit writes under the controller's classification.
                try:
                    session = open_composition_session(streaming_committer)
                    worker.composition_active = session is not None
                    if session is not None:
                        # Irreversible marker: once a preedit was bound to a
                        # focused context, uncertain outcomes can never fall
                        # back to a fresh full-sentence commit elsewhere.
                        worker.composition_started = True
                except CompositionRefusedError as exc:
                    # The backend SUPPORTS composition but refused: the user
                    # is composing (Rime preedit), there is no focused
                    # non-sensitive context, or the reply was lost. This is
                    # NOT "no composition capability" — a plain CommitText
                    # fallback would clobber the user's preedit or write
                    # into a new focus. Terminal "suppressed": zero writes
                    # for this utterance; ASR keeps running so the text can
                    # still be shown for manual copy.
                    worker.composition_refused = True
                    worker.outcome = "suppressed"
                    worker.commit_info = {
                        "backend": "fcitx",
                        "committed": False,
                        "detail": f"realtime_composition_refused:{exc}",
                        "outcome": "suppressed",
                    }
                    on_state(
                        {
                            "event": "log",
                            "message": (
                                "realtime_composition_refused: "
                                f"{exc} — 会话被拒绝（用户预编辑/无焦点/敏感/超时），"
                                "本次不做任何本地提交或退格"
                            ),
                        }
                    )
                    session = None
                    _fatal("composition_refused")
                except Exception as exc:  # noqa: BLE001
                    on_state(
                        {
                            "event": "log",
                            "message": (
                                "realtime_composition_unavailable: "
                                f"{type(exc).__name__}: {exc} — streaming preview-only"
                            ),
                        }
                    )
                    session = None
            elif enable_local_commit and cancel_event.is_set():
                on_state(
                    {
                        "event": "log",
                        "message": (
                            "realtime_composition_cancelled_before_begin: "
                            "deadline/cancel 在 Begin 之前到达 — 不再绑定新会话，"
                            "本次不写任何 preedit"
                        ),
                    }
                )
            if (
                enable_local_commit
                and session is None
                and bool(getattr(args, "debug_diagnostics", False))
            ):
                on_state(
                    {
                        "event": "log",
                        "message": (
                            "realtime_composition_absent "
                            f"backend={getattr(streaming_committer, 'backend_name', 'unknown')} "
                            "— preview-only, final commit by postprocess pipeline"
                        ),
                    }
                )

            def _build_streaming_corrector(context: str = "") -> Any:
                """Bounded streaming corrector (deterministic + SemIf judge).

                Deterministic snapshot on submit, same-snapshot result on
                poll, one end-of-sentence budget on finish. Constructed with
                the session hotwords plus the args-level replacement lexicon
                so "错词→正词" pairs apply during streaming too. ``context``
                is the bounded previous-segment tail, judgment input only.
                """
                effective = list(resolve_hotwords())
                _lex, pairs = lexicon_from_args(args)
                if not effective:
                    effective = list(_lex)
                for src, dst in pairs or []:
                    effective.append(f"{src}→{dst}")
                seen: set[str] = set()
                merged: list[str] = []
                for t in effective:
                    if t in seen:
                        continue
                    seen.add(t)
                    merged.append(t)
                bounded_context = str(context or "")[-CONTINUOUS_CONTEXT_CHARS:]
                # Same factory as the final path so contextual_aliases are not
                # dropped on the realtime side. Attribute lookup stays on the
                # module so tests can swap the factory or the class it builds.
                factory = getattr(_streaming_correction, "corrector_from_args", None)
                if callable(factory):
                    return factory(args, merged, context=bounded_context)
                kwargs: dict[str, Any] = {
                    "endpoint": str(getattr(args, "semif_endpoint", "") or ""),
                    "timeout_s": float(getattr(args, "semif_timeout_s", 0.12) or 0.12),
                    "enabled": bool(getattr(args, "enable_semif_correction", False)),
                }
                params: Mapping[str, inspect.Parameter]
                try:
                    params = inspect.signature(
                        _streaming_correction.StreamingHotwordCorrector
                    ).parameters
                except (TypeError, ValueError):
                    params = {}
                if not params or "context" in params:
                    kwargs["context"] = bounded_context
                if (not params or "contextual_aliases" in params) and hasattr(
                    args, "contextual_aliases"
                ):
                    kwargs["contextual_aliases"] = getattr(args, "contextual_aliases", None)
                return _streaming_correction.StreamingHotwordCorrector(merged, **kwargs)

            if (
                session is not None
                and bool(getattr(session, "supports_segments", False))
                and callable(getattr(session, "commit_segment", None))
                and getattr(provider, "provider_name", "") == "confucius-asr"
            ):
                worker.continuous = True
                from .continuous_dictation import run_continuous_dictation

                run_continuous_dictation(
                    worker=worker,
                    session=session,
                    provider=provider,
                    reader=reader,
                    cancel_event=cancel_event,
                    args=args,
                    chunk_bytes=chunk_bytes,
                    sample_rate=sample_rate,
                    channels=channels,
                    build_corrector=_build_streaming_corrector,
                    resolve_hotwords=resolve_hotwords,
                    normalize_final_text=normalize_final_text,
                    on_state=on_state,
                    on_capture_fatal=_fatal,
                    refine_enabled=refine_enabled,
                    auto_hard_enter=auto_hard_enter,
                    streaming_committer=streaming_committer,
                )
                return

            session_hotwords = resolve_hotwords()
            corrector = _build_streaming_corrector()
            asr_session = provider.start_realtime_session(hotwords=session_hotwords)
            worker.cancel_session = lambda: _cancel_asr_session(asr_session)

            stale = False
            last_displayed = ""

            def _show_partial(response: dict[str, object]) -> None:
                nonlocal stale, last_displayed
                current_text = normalize_final_text(str(response.get("text", "")))
                if not current_text:
                    return
                if current_text == worker.partial_text:
                    # Same ASR snapshot: a ready SemIf judgment may have
                    # landed since the last display.
                    corrected = corrector.poll(current_text)
                else:
                    worker.partial_text = current_text
                    corrected = corrector.submit(current_text)
                if not corrected or corrected == last_displayed:
                    return
                last_displayed = corrected
                on_state(
                    {
                        "event": "realtime_asr_partial",
                        "text": corrected,
                        "metadata": response,
                    }
                )
                if session is not None and session.active and not cancel_event.is_set():
                    result = session.update_preedit(corrected)
                    detail = str(getattr(result, "detail", ""))
                    if not bool(getattr(result, "committed", False)):
                        if str(getattr(result, "outcome", "")) == "stale" or "preedit_stale" in detail:
                            stale = True
                            worker.session_stale = True
                            on_state(
                                {
                                    "event": "log",
                                    "message": (
                                        "realtime_session_stale: focus lost or user typed — "
                                        "stopping stream, no fallback commit"
                                    ),
                                }
                            )
                        else:
                            on_state(
                                {
                                    "event": "log",
                                    "message": f"realtime_preedit_update_failed: {detail}",
                                }
                            )

            while True:
                if cancel_event.is_set() or stale:
                    break
                raw = reader.read(chunk_bytes)
                if not raw:
                    break
                while len(raw) < chunk_bytes:
                    more = reader.read(chunk_bytes - len(raw))
                    if not more:
                        break
                    raw += more
                if cancel_event.is_set() or stale:
                    break
                response = asr_session.push_audio(raw)
                _show_partial(response)

            if cancel_event.is_set() or stale:
                # stop/cancel/timeout, or the bound context disappeared:
                # cancel everything, commit nothing.
                try:
                    asr_session.cancel()
                except Exception:  # noqa: BLE001
                    pass
                if corrector is not None:
                    try:
                        corrector.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                if session is not None:
                    try:
                        session.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                reason = "realtime_session_stale" if stale else "realtime_cancelled"
                outcome = "stale" if stale else "cancelled"
                worker.outcome = outcome
                worker.final_text = ""
                if worker.commit_info is None:
                    worker.commit_info = {
                        "backend": "fcitx" if session is not None else str(
                            getattr(streaming_committer, "backend_name", "unknown")
                        ),
                        "committed": False,
                        "detail": reason,
                        "outcome": outcome,
                    }
                return

            final_result = asr_session.finish()
            final_raw_text = normalize_final_text(str(getattr(final_result, "text", "")))
            # Single bounded end-of-sentence budget. ``finish`` submits the
            # final snapshot first (a final_raw different from the last
            # partial still gets its SemIf judgment) without resetting any
            # in-flight task for the same snapshot, then waits once.
            worker.final_text = corrector.finish(final_raw_text)
            worker.semif_applied = True
            worker.detected_language = str(getattr(final_result, "detected_language", "") or "").strip()
            worker.transcribe_latency_ms = asr_session.elapsed_ms

            if session is None:
                if worker.composition_refused:
                    # Refused ≠ unsupported: keep the terminal "suppressed"
                    # outcome and commit_info set above. The transcript stays
                    # in final_text for the UI (manual copy); the pipeline
                    # must not commit, backspace, or fall back in any way.
                    return
                # Legacy backend: preview-only. The postprocess pipeline owns
                # the single final commit.
                worker.outcome = "no_composition"
                worker.commit_info = None
                return

            if refine_enabled:
                # Keep the composition session bound to the original input
                # context: the pipeline refines and then commits through this
                # exact token. Cancelling here would orphan the binding and
                # force a fresh commit into whatever has focus later.
                worker.composition_session = session
                worker.outcome = "released_for_refine"
                worker.commit_info = {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": "realtime_preedit_released_for_refine",
                    "outcome": "released_for_refine",
                }
                return

            final_text = worker.final_text.strip()
            if not final_text:
                session.cancel()
                worker.outcome = "cancelled"
                worker.commit_info = {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": "realtime_empty_final",
                    "outcome": "cancelled",
                }
                return

            if cancel_event.is_set():
                session.cancel()
                worker.outcome = "cancelled"
                worker.commit_info = {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": "realtime_cancelled_before_commit",
                    "outcome": "cancelled",
                }
                return

            # Liveness probe with the final text: if the context lost focus
            # between the last (possibly deduped) partial and here, this
            # fails the session without ever attempting the commit.
            probe = session.update_preedit(final_text)
            if not bool(getattr(probe, "committed", False)):
                # Fail closed: a stale probe (focus lost / reset / user
                # typed) or even a transient transport failure means the
                # liveness of the bound context could not be established —
                # never attempt the commit after it.
                probe_outcome = str(getattr(probe, "outcome", "")) or "uncertain"
                stale_probe = probe_outcome == "stale"
                if stale_probe:
                    worker.session_stale = True
                    worker.final_text = ""
                worker.outcome = probe_outcome
                worker.commit_info = {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": (
                        "realtime_session_stale"
                        if stale_probe
                        else f"realtime_probe_failed:{getattr(probe, 'detail', '')}"
                    ),
                    "outcome": probe_outcome,
                }
                on_state(
                    {
                        "event": "log",
                        "message": (
                            "realtime_session_stale: focus lost or user typed — "
                            "stopping stream, no fallback commit"
                            if stale_probe
                            else (
                                "realtime_probe_failed: "
                                f"{getattr(probe, 'detail', '')} — no commit attempt"
                            )
                        ),
                    }
                )
                return

            commit_result = session.commit(final_text)
            committed = bool(getattr(commit_result, "committed", False))
            detail = str(getattr(commit_result, "detail", ""))
            outcome = str(getattr(commit_result, "outcome", "")) or (
                "committed" if committed else "uncertain"
            )
            if not committed and outcome == "stale":
                # Staleness only surfaced at commit time (e.g. deduped
                # partials skipped the failing preedit update). The
                # utterance was NOT written: clear the final text so no
                # downstream stage can treat it as committed-able output.
                worker.session_stale = True
                worker.final_text = ""
                detail = "realtime_session_stale"
            worker.outcome = outcome
            worker.commit_info = {
                "backend": "fcitx",
                "committed": committed,
                "detail": detail or "realtime_composition_commit",
                "outcome": outcome,
            }
            if committed and auto_hard_enter:
                enter_delay_s = paste_to_enter_delay_seconds(commit_result)
                if enter_delay_s > 0.0:
                    time.sleep(enter_delay_s)
                enter_result = send_hard_enter(streaming_committer)
                enter_detail = str(getattr(enter_result, "detail", "")).strip()
                if enter_detail:
                    worker.commit_info["detail"] = f"{detail};{enter_detail}"
        except Exception as exc:  # noqa: BLE001
            worker.error = f"{type(exc).__name__}: {exc}"
            # Never trust the partial as a final transcript, and never
            # commit after a failure.
            worker.final_text = ""
            if asr_session is not None:
                try:
                    asr_session.cancel()
                except Exception:  # noqa: BLE001
                    pass
            if corrector is not None:
                try:
                    corrector.cancel()
                except Exception:  # noqa: BLE001
                    pass
            if session is not None:
                try:
                    session.cancel()
                except Exception:  # noqa: BLE001
                    pass
            if worker.commit_info is None:
                # A composition session may have been bound when the worker
                # died: the exception may even have happened while (or after)
                # the commit call was in flight, so the write state is
                # unknown. This is terminal — never fall back to a fresh
                # commit elsewhere. Preview-only failures (no composition)
                # may safely fall back to a full-audio transcription.
                outcome = "uncertain" if worker.composition_started else "no_composition"
                worker.outcome = outcome
                worker.commit_info = {
                    "backend": "fcitx" if session is not None else str(
                        getattr(streaming_committer, "backend_name", "unknown")
                    ),
                    "committed": False,
                    "detail": f"realtime_failed:{worker.error}",
                    "outcome": outcome,
                }
        finally:
            # composition_started is deliberately NOT cleared here: it is
            # the controller's irreversible "a preedit was once bound"
            # marker and must survive the worker teardown.
            worker.composition_active = False
            if corrector is not None:
                try:
                    corrector.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                reader.close()
            except Exception:
                pass

    worker.thread = threading.Thread(target=_run, name="recordian-realtime-asr", daemon=True)
    worker.thread.start()
    return worker
