"""Sample-counted Confucius segments on one microphone and one IME token.

The server keeps its 30s sample cap. This module never opens a second
recorder and never calls BeginSession again. Segment text goes through
``commit_segment``; the user-stop tail goes through ``CommitSession`` once.

Raw seam rule: ASR text stays RAW until after the held-tail split/join.
Formatting (corrector.finish) and normalisation (spoken numbers/URLs) run
only on the text that is displayed or committed, never on the held tail —
so raw 一百二 + 十五 still formats as 125 and www点exa + mple点com as
www.example.com.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from . import duration_guard as guard
from .linux_dictate import MonitorOverflowError
from .realtime_asr import _pcm_rms, _split_held_tail

_HISTORY_CHARS = 262144
_PUSH_WAIT_S = 8.0


def join_raw_tail(held_raw: str, hypothesis: str) -> str:
    """Join an uncommitted raw tail to the next raw hypothesis.

    Numbers, IPs, Chinese numerals and spoken domains concatenate. A held
    English word gains one space before another English word. Nothing is
    deduped.
    """
    held = held_raw or ""
    nxt = hypothesis or ""
    if not held:
        return nxt
    if not nxt:
        return held
    if nxt[0].isspace():
        return held + nxt
    if _split_held_tail(held)[0] == "" and not _is_ascii_word(held):
        return held + nxt
    if _is_ascii_word(held) and nxt[0].isascii() and nxt[0].isalpha():
        return held + " " + nxt
    return held + nxt


def _is_ascii_word(text: str) -> bool:
    if not text:
        return False
    return all(("A" <= ch <= "Z") or ("a" <= ch <= "z") or ch == "'" for ch in text)


def run_continuous_dictation(
    *,
    worker: Any,
    session: Any,
    provider: Any,
    reader: Any,
    cancel_event: Any,
    args: Any,
    chunk_bytes: int,
    sample_rate: int,
    channels: int,
    build_corrector: Callable[[str], Any],
    resolve_hotwords: Callable[[], list[str]],
    normalize_final_text: Callable[[str], str],
    on_state: Callable[[dict[str, object]], None],
    on_capture_fatal: Callable[[str], None] | None,
    refine_enabled: bool,
    auto_hard_enter: bool,
    streaming_committer: Any,
) -> None:
    """Rotate ASR sockets until the reader EOFs or the turn fails closed."""
    from .linux_commit import paste_to_enter_delay_seconds, send_hard_enter

    worker.continuous = True
    bps = 4 * max(1, int(channels))
    rate = max(1, int(sample_rate))
    held_raw = ""
    context_tail = ""
    segment_samples = 0
    silence_run = 0
    pending = bytearray()
    eof = False
    asr: Any = None
    asr_push_blocking = False
    corrector: Any = None
    segment_index = 0
    committed_chars_total = 0
    failed = False
    live = {"asr": None}
    # Audio-counted IME inactivity: the composition token dies after 120 s
    # idle, so long silence must re-touch it well before that.
    ime_idle_samples = 0
    last_display = ""
    # Last RAW snapshot handed to submit(); repeating it must poll for a ready
    # judgment instead of submitting again.
    last_partial_snapshot: str | None = None

    spool = NamedTemporaryFile(
        prefix="recordian-continuous-",
        suffix=".txt",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    worker.transcript_path = spool.name

    def _fatal(reason: str) -> None:
        if cancel_event.is_set() or not callable(on_capture_fatal):
            return
        try:
            on_capture_fatal(str(reason))
        except Exception:  # noqa: BLE001
            pass

    def _fail(reason: str, *, outcome: str) -> None:
        nonlocal failed, asr, corrector
        failed = True
        if cancel_event.is_set() and outcome not in {"stale"}:
            # A controller cancel (deadline/exit) outranks the local failure
            # classification: the turn was cancelled, not lost.
            outcome = "cancelled"
            reason = "realtime_cancelled"
        worker.error = reason
        worker.outcome = outcome
        prefix_kept = int(getattr(worker, "segments_committed", 0) or 0) > 0
        worker.commit_info = {
            "backend": "fcitx",
            "committed": prefix_kept,
            "detail": reason if not prefix_kept else f"continuous_prefix_kept;{reason}",
            "outcome": "committed" if prefix_kept else outcome,
            "segments_committed": int(getattr(worker, "segments_committed", 0) or 0),
        }
        if outcome == "stale":
            worker.session_stale = True
        if asr is not None:
            try:
                asr.cancel()
            except Exception:  # noqa: BLE001
                pass
            asr = None
            live["asr"] = None
        if corrector is not None:
            try:
                corrector.close()
            except Exception:  # noqa: BLE001
                pass
            corrector = None
        # Always retract the uncommitted preedit tail, even when a committed
        # prefix is preserved: commit_segment results stay, the dangling
        # token must not leak a live preedit. Best-effort, never a retry.
        try:
            session.cancel()
        except Exception:  # noqa: BLE001
            pass
        _fatal(reason)

    def _cancel_live() -> None:
        cancel_event.set()
        current = live["asr"]
        if current is not None:
            try:
                current.cancel()
            except Exception:  # noqa: BLE001
                pass

    worker.cancel_session = _cancel_live

    def _limits() -> tuple[int, int, int]:
        return (
            int(guard.CONTINUOUS_SEGMENT_MIN_S * rate),
            int(guard.CONTINUOUS_SEGMENT_MAX_S * rate),
            max(1, int(guard.CONTINUOUS_SILENCE_S * rate)),
        )

    def _open_asr() -> Any:
        """Open the next socket. Parameters are chosen by signature
        inspection BEFORE the call — never by catching TypeError after it
        (a TypeError from inside the function body would otherwise trigger
        a duplicate open)."""
        nonlocal asr_push_blocking
        hotwords = list(resolve_hotwords() or [])
        fn = provider.start_realtime_session
        kwargs: dict[str, Any] = {"hotwords": hotwords}
        params: Mapping[str, inspect.Parameter]
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        prefix = context_tail[-guard.CONTINUOUS_CONTEXT_CHARS :]
        if not params or "prefix_context" in params:
            kwargs["prefix_context"] = prefix
        if not params or "cancel_event" in params:
            kwargs["cancel_event"] = cancel_event
        opened = fn(**kwargs)
        push_params: Mapping[str, inspect.Parameter]
        try:
            push_params = inspect.signature(opened.push_audio).parameters
        except (TypeError, ValueError, AttributeError):
            push_params = {}
        asr_push_blocking = not push_params or "block" in push_params
        live["asr"] = opened
        return opened

    def _push(piece: bytes) -> dict[str, object]:
        """Enqueue one slice on the CURRENT socket. Bounded blocking
        backpressure on the same accepted connection is allowed; a frame
        that timed out was not accepted, so re-offering it is safe. The
        call shape is fixed at open time — no post-hoc TypeError retry."""
        assert asr is not None
        deadline = time.monotonic() + _PUSH_WAIT_S
        while True:
            if cancel_event.is_set():
                raise RuntimeError("cancelled")
            try:
                if asr_push_blocking:
                    response = asr.push_audio(piece, block=True, timeout_s=0.5)
                else:
                    response = asr.push_audio(piece)
            except TimeoutError:
                # queue.put timed out: the frame was NOT accepted.
                if time.monotonic() >= deadline:
                    raise TimeoutError("asr send backpressure") from None
                continue
            return response if isinstance(response, dict) else {"text": ""}

    def _close_corrector() -> None:
        nonlocal corrector
        if corrector is None:
            return
        try:
            corrector.close()
        except Exception:  # noqa: BLE001
            pass
        corrector = None

    def _ensure_corrector() -> Any:
        nonlocal corrector
        if corrector is None:
            corrector = build_corrector(context_tail[-guard.CONTINUOUS_CONTEXT_CHARS :])
        return corrector

    def _preedit(text: str, *, refresh: bool = False) -> bool:
        """Show (or re-touch) the bounded live preedit on the SAME token.

        ``refresh=True`` is the TTL keepalive during long silence: it sends
        the current (possibly empty) preedit again — no new text, no new
        BeginSession. A cancel observed here means zero further writes.
        """
        nonlocal ime_idle_samples, last_display
        if cancel_event.is_set():
            _fail("realtime_cancelled", outcome="cancelled")
            return False
        shown = text[-guard.CONTINUOUS_PREEDIT_CHARS :]
        worker.partial_text = shown
        last_display = shown
        if not shown and not refresh:
            return True
        result = session.update_preedit(shown)
        if bool(getattr(result, "committed", False)):
            ime_idle_samples = 0
            return True
        outcome = str(getattr(result, "outcome", "") or "")
        if outcome == "stale" or "preedit_stale" in str(getattr(result, "detail", "")):
            _fail("ime_stale", outcome="stale")
        else:
            _fail(f"ime_preedit_failed:{getattr(result, 'detail', '')}", outcome="uncertain")
        return False

    def _note(canonical: str) -> None:
        if canonical:
            spool.write(canonical)
            spool.flush()

    def _emit(index: int, samples: int, committed_chars: int) -> None:
        on_state(
            {
                "event": "segment_end",
                "index": index,
                "audio_samples": samples,
                "duration": samples / float(rate),
                "committed_chars": committed_chars,
            }
        )

    def _commit_piece(prefix_raw: str) -> bool:
        nonlocal context_tail, committed_chars_total, ime_idle_samples, last_display
        nonlocal last_partial_snapshot
        if not prefix_raw:
            return True
        formatter = _ensure_corrector()
        canonical = normalize_final_text(str(formatter.finish(prefix_raw) or ""))
        worker.semif_applied = True
        _close_corrector()
        if not canonical:
            return True
        # The finish() wait may have overlapped a controller cancel: zero new
        # preedit writes and zero segment commits afterwards.
        if cancel_event.is_set():
            _fail("realtime_cancelled", outcome="cancelled")
            return False
        if not _preedit(canonical):
            return False
        if cancel_event.is_set():
            _fail("realtime_cancelled", outcome="cancelled")
            return False
        result = session.commit_segment(canonical)
        if not bool(getattr(result, "committed", False)):
            outcome = str(getattr(result, "outcome", "") or "uncertain")
            if outcome not in {"stale", "uncertain"}:
                outcome = "uncertain"
            _fail(f"segment_commit_{outcome}", outcome=outcome)
            return False
        ime_idle_samples = 0
        # commit_segment consumed the whole preedit: the committed prefix is
        # no longer part of the composition token, so the long-silence
        # keepalive must never replay it (it would duplicate text on a GTK
        # focus change). Only uncommitted text may stay in last_display.
        last_display = ""
        # The corrector was closed above and the next segment rebuilds one:
        # its first partial must submit again even if the string repeats.
        last_partial_snapshot = None
        worker.segments_committed = int(worker.segments_committed) + 1
        committed_chars_total += len(canonical)
        context_tail = (context_tail + canonical)[-guard.CONTINUOUS_CONTEXT_CHARS :]
        _note(canonical)
        return True

    def _finish_asr() -> str:
        """EOS the current socket and return its RAW final text.

        No normalisation here: the raw text must survive unchanged until
        after the held-tail split/join, otherwise numeric/URL seams break.
        """
        nonlocal asr
        if asr is None:
            return ""
        closing = asr
        if cancel_event.is_set():
            try:
                closing.cancel()
            except Exception:  # noqa: BLE001
                pass
            asr = None
            live["asr"] = None
            return ""
        result = closing.finish()
        asr = None
        live["asr"] = None
        language = str(getattr(result, "detected_language", "") or "").strip()
        if language:
            worker.detected_language = language
        try:
            worker.transcribe_latency_ms += float(getattr(closing, "elapsed_ms", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        return str(getattr(result, "text", "") or "")

    def _rotate(*, final: bool) -> bool:
        nonlocal held_raw, segment_samples, silence_run, segment_index, ime_idle_samples
        nonlocal last_display
        samples = segment_samples
        segment_samples = 0
        silence_run = 0
        if cancel_event.is_set() and not final:
            _fail("realtime_cancelled", outcome="cancelled")
            return False
        try:
            asr_text = _finish_asr()
        except Exception as exc:  # noqa: BLE001
            _fail(f"asr_session_failed:{type(exc).__name__}", outcome="uncertain")
            return False
        # finish() blocks: re-check cancellation after it, even at EOF — a
        # controller deadline must never be followed by preedit/commit writes.
        if cancel_event.is_set():
            _fail("realtime_cancelled", outcome="cancelled")
            return False
        snapshot = join_raw_tail(held_raw, asr_text)
        segment_index += 1
        if not final:
            prefix_raw, held_raw = _split_held_tail(snapshot)
            if not _commit_piece(prefix_raw):
                return False
            _emit(segment_index, samples, committed_chars_total)
            return True
        # User stop (reader EOF): one formatter pass over the whole
        # uncommitted raw tail, then normalise once for display/commit.
        _close_corrector()
        formatter = build_corrector(context_tail[-guard.CONTINUOUS_CONTEXT_CHARS :])
        canonical = normalize_final_text(str(formatter.finish(snapshot) or "")) if snapshot else ""
        try:
            formatter.close()
        except Exception:  # noqa: BLE001
            pass
        worker.semif_applied = bool(snapshot)
        held_raw = ""
        if cancel_event.is_set():
            _fail("realtime_cancelled", outcome="cancelled")
            return False
        if refine_enabled and int(worker.segments_committed) == 0 and canonical.strip():
            _note(canonical)
            worker.composition_session = session
            worker.outcome = "released_for_refine"
            worker.commit_info = {
                "backend": "fcitx",
                "committed": False,
                "detail": "realtime_preedit_released_for_refine",
                "outcome": "released_for_refine",
            }
            _emit(segment_index, samples, 0)
            return True
        if not canonical.strip():
            try:
                session.cancel()
            except Exception:  # noqa: BLE001
                pass
            if int(worker.segments_committed) > 0:
                worker.outcome = "committed"
                worker.commit_info = {
                    "backend": "fcitx",
                    "committed": True,
                    "detail": "continuous_tail_empty",
                    "outcome": "committed",
                    "segments_committed": int(worker.segments_committed),
                }
            else:
                worker.outcome = "cancelled"
                worker.commit_info = {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": "realtime_empty_final",
                    "outcome": "cancelled",
                }
            _emit(segment_index, samples, committed_chars_total)
            return True
        # Cancel check BEFORE the final preedit: a deadline landing during
        # the formatter wait must produce zero further writes.
        if cancel_event.is_set():
            _fail("realtime_cancelled_before_commit", outcome="cancelled")
            return False
        if not _preedit(canonical):
            return False
        if cancel_event.is_set():
            _fail("realtime_cancelled_before_commit", outcome="cancelled")
            return False
        result = session.commit(canonical)
        committed = bool(getattr(result, "committed", False))
        outcome = str(getattr(result, "outcome", "") or ("committed" if committed else "uncertain"))
        if not committed:
            if outcome not in {"stale", "uncertain"}:
                outcome = "uncertain"
            _fail(f"final_commit_{outcome}", outcome=outcome)
            return False
        ime_idle_samples = 0
        _note(canonical)
        # The final commit consumed the preedit; nothing of it may be
        # replayed by a (hypothetical) later refresh.
        last_display = ""
        detail = "continuous_final"
        if refine_enabled and int(worker.segments_committed) > 0:
            detail = "continuous_final;continuous_refine_suppressed"
        worker.outcome = "committed"
        worker.commit_info = {
            "backend": "fcitx",
            "committed": True,
            "detail": detail,
            "outcome": "committed",
            "segments_committed": int(worker.segments_committed),
        }
        if auto_hard_enter:
            try:
                delay = paste_to_enter_delay_seconds(result)
                if delay > 0:
                    time.sleep(delay)
                send_hard_enter(streaming_committer)
            except Exception:  # noqa: BLE001
                pass
        _emit(segment_index, samples, committed_chars_total + len(canonical))
        return True

    try:
        while not failed:
            if cancel_event.is_set() and not eof:
                _fail("realtime_cancelled", outcome="cancelled")
                return
            if len(pending) < bps and not eof:
                # A short read may leave a partial f32 frame in ``pending``:
                # keep reading until one full frame is buffered (or EOF), so
                # a sub-frame read can never spin the loop on ``have <= 0``
                # with a non-empty buffer. Bytes are only ever appended, so
                # no sample is duplicated or dropped.
                try:
                    raw = reader.read(chunk_bytes)
                except MonitorOverflowError:
                    _fail("monitor_backlog_overflow", outcome="uncertain")
                    return
                if not raw:
                    eof = True
                else:
                    pending.extend(raw)
            if not pending and eof:
                if asr is None and segment_samples == 0:
                    if not _rotate(final=True):
                        return
                    return
                if not _rotate(final=True):
                    return
                return
            min_samples, max_samples, silence_need = _limits()
            if max_samples <= 0:
                max_samples = min_samples or 1
            if segment_samples >= max_samples and pending:
                if not _rotate(final=False):
                    return
                continue
            if asr is None:
                try:
                    asr = _open_asr()
                except Exception as exc:  # noqa: BLE001
                    _fail(f"asr_session_failed:{type(exc).__name__}", outcome="uncertain")
                    return
            room = max(0, max_samples - segment_samples)
            have = len(pending) // bps
            if have <= 0:
                if eof:
                    pending.clear()
                    if not _rotate(final=True):
                        return
                    return
                continue
            take = min(have, room) if room else 0
            if take <= 0:
                if not _rotate(final=False):
                    return
                continue
            nbytes = take * bps
            piece = bytes(pending[:nbytes])
            del pending[:nbytes]
            try:
                response = _push(piece)
            except Exception as exc:  # noqa: BLE001
                _fail(f"asr_session_failed:{type(exc).__name__}", outcome="uncertain")
                return
            segment_samples += take
            ime_idle_samples += take
            if _pcm_rms(piece) < float(guard.CONTINUOUS_SILENCE_RMS):
                silence_run += take
            else:
                silence_run = 0
            raw_partial = str(response.get("text", "") or "")
            if raw_partial or held_raw:
                formatter = _ensure_corrector()
                display = join_raw_tail(held_raw, raw_partial)
                try:
                    if last_partial_snapshot is not None and display == last_partial_snapshot:
                        # Same original snapshot as the last submit(): submit()
                        # can only return the deterministic text, but a
                        # SemIf/Jev judgment started for this exact snapshot
                        # may be ready now. Poll it so the correction reaches
                        # the preedit before finish()/the segment boundary.
                        corrected = formatter.poll(display)
                    else:
                        last_partial_snapshot = display
                        corrected = formatter.submit(display)
                    display = str(corrected or display)
                except Exception:  # noqa: BLE001
                    pass
                # Normalise for DISPLAY only; held_raw stays raw.
                if not _preedit(normalize_final_text(display)):
                    return
            # TTL keepalive: the composition token expires after 120 s of
            # IME inactivity. Long silence commits nothing, so re-touch the
            # same token with the current (possibly empty) preedit.
            if ime_idle_samples >= int(guard.CONTINUOUS_IME_REFRESH_S * rate):
                if not _preedit(last_display, refresh=True):
                    return
            hard = segment_samples >= max_samples
            soft = segment_samples >= min_samples and silence_run >= silence_need
            if hard or soft or (eof and not pending):
                if not _rotate(final=bool(eof and not pending)):
                    return
                if eof and not pending:
                    return
    except Exception as exc:  # noqa: BLE001
        # Unexpected worker failure (preedit raise, reader OSError, …): route
        # through the same fail-closed path so the live socket is cancelled,
        # the IME preedit is retracted, and the fatal callback stops the mic.
        if not failed:
            _fail(f"continuous_unexpected:{type(exc).__name__}", outcome="uncertain")
    finally:
        # Bounded read: at most _HISTORY_CHARS+1 are loaded. Truncation
        # policy keeps the FIRST characters (the committed prefix is never
        # altered); only unbounded tail history is dropped.
        text = ""
        try:
            spool.flush()
            with open(spool.name, encoding="utf-8") as fh:
                text = fh.read(_HISTORY_CHARS + 1)
        except OSError:
            text = ""
        worker.final_text = text[:_HISTORY_CHARS]
        try:
            spool.close()
        except Exception:  # noqa: BLE001
            pass
        # The spool is read once on stop; never leave per-turn files behind.
        try:
            Path(spool.name).unlink(missing_ok=True)
        except OSError:
            pass
        worker.transcript_path = ""
        _close_corrector()
