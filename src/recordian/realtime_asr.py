"""Realtime ASR worker components.

Provides the incremental commit accumulator and the worker thread that
drives a streaming/realtime ASR session, feeding partial results to the
accumulator and producing the final transcription.
"""
from __future__ import annotations

import argparse
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from recordian.text_cleanup import (
    _optimistic_first_partial,
    _stable_prefix_delta,
)

# NOTE: These imports will be replaced by state_manager imports once that
#       module is available.  For now they are passed in as parameters.
from .linux_commit import (
    paste_to_enter_delay_seconds,
    resolve_streaming_committer,
    send_hard_enter,
)
from .linux_dictate import open_monitor_stream_reader
from .providers import provider_supports_realtime

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
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
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
        if final_text.startswith(self.committed_text):
            tail = final_text[len(self.committed_text):]
            if tail:
                self.append_text(tail)
                self._flush_pending()
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
) -> _RealtimeASRWorkerHandle | None:
    """Start a background thread that drives a realtime ASR session.

    The worker reads audio chunks from the record handle's monitor stream,
    pushes them to the provider's realtime session, and forwards partial
    transcripts to the accumulator for incremental commit.

    Parameters
    ----------
    args:
        Parsed CLI arguments / runtime configuration namespace.
    provider:
        ASR provider object (must support ``start_realtime_session``).
    record_handle:
        Handle returned by the recording subsystem; must expose a monitor
        stream reader via ``open_monitor_stream_reader``.
    committer:
        The text committer used for streaming results.
    enable_local_commit:
        Whether incremental (mid-utterance) commits are allowed.
    auto_hard_enter:
        Whether to press Enter automatically after the final commit.
    resolve_hotwords:
        Callable returning the current hotword list for the session.
    normalize_final_text:
        Callable that normalizes raw transcription text.
    on_state:
        Callback invoked with state-dict events (partial results, logs).

    Returns
    -------
    _RealtimeASRWorkerHandle | None
        A handle for the running worker, or ``None`` if realtime ASR is
        not supported or not enabled.
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

    def _run() -> None:
        session = None
        streaming_committer = resolve_streaming_committer(committer)
        supports_realtime_local_commit = (
            enable_local_commit
            and str(getattr(streaming_committer, "backend_name", "")).strip().lower() != "xdotool-clipboard"
        )
        accumulator = _RealtimeCommitAccumulator(streaming_committer) if supports_realtime_local_commit else None
        preview_text = ""
        last_hypothesis = ""
        committed_preview = ""
        try:
            if (
                enable_local_commit
                and bool(getattr(args, "debug_diagnostics", False))
                and streaming_committer is not committer
            ):
                on_state(
                    {
                        "event": "log",
                        "message": (
                            "diag realtime_streaming_committer "
                            f"from={getattr(committer, 'backend_name', 'unknown')} "
                            f"to={getattr(streaming_committer, 'backend_name', 'unknown')}"
                        ),
                    }
                )
            session = provider.start_realtime_session(hotwords=resolve_hotwords())
            while True:
                raw = reader.read(chunk_bytes)
                if not raw:
                    break
                response = session.push_audio(raw)
                current_text = normalize_final_text(str(response.get("text", "")))
                if current_text and current_text != preview_text:
                    preview_text = current_text
                    on_state(
                        {
                            "event": "realtime_asr_partial",
                            "text": current_text,
                            "metadata": response,
                        }
                    )
                    if accumulator is not None:
                        if not committed_preview and not last_hypothesis:
                            optimistic = _optimistic_first_partial(current_text)
                            committed_preview = optimistic
                            delta = optimistic
                        else:
                            committed_preview, delta = _stable_prefix_delta(
                                previous_hypothesis=last_hypothesis,
                                committed_text=committed_preview,
                                current_hypothesis=current_text,
                            )
                        if delta:
                            accumulator.append_text(delta)
                    last_hypothesis = current_text
            final_result = session.finish()
            worker.final_text = normalize_final_text(final_result.text)
            worker.detected_language = str(getattr(final_result, "detected_language", "") or "").strip()
            worker.transcribe_latency_ms = session.elapsed_ms
            if accumulator is not None:
                worker.commit_info = accumulator.finalize(
                    final_text=worker.final_text, auto_hard_enter=auto_hard_enter
                )
        except Exception as exc:  # noqa: BLE001
            worker.error = f"{type(exc).__name__}: {exc}"
            if session is not None:
                session.cancel()
        finally:
            try:
                reader.close()
            except Exception:
                pass

    worker.thread = threading.Thread(target=_run, name="recordian-realtime-asr", daemon=True)
    worker.thread.start()
    return worker
