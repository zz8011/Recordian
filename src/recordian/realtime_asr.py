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



# NOTE: These imports will be replaced by state_manager imports once that
#       module is available.  For now they are passed in as parameters.
from .linux_commit import (
    paste_to_enter_delay_seconds,
    resolve_streaming_committer,
    send_backspaces,
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
    cancel_session: Callable[[], None] | None = None


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
        except Exception as exc:  # noqa: BLE001
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
            and bool(getattr(args, "enable_streaming_commit", False))
            and str(getattr(streaming_committer, "backend_name", "")).strip().lower() != "xdotool-clipboard"
        )
        accumulator = _RealtimeCommitAccumulator(streaming_committer) if supports_realtime_local_commit else None
        preview_text = ""
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
            session_hotwords = resolve_hotwords()
            if accumulator is not None:
                accumulator.hotwords = list(session_hotwords)
            session = provider.start_realtime_session(hotwords=session_hotwords)
            worker.cancel_session = session.cancel

            def _show_partial(response: dict[str, object]) -> None:
                nonlocal preview_text
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
                        accumulator.sync_to(current_text)

            while True:
                raw = reader.read(chunk_bytes)
                if not raw:
                    break
                while len(raw) < chunk_bytes:
                    more = reader.read(chunk_bytes - len(raw))
                    if not more:
                        break
                    raw += more
                response = session.push_audio(raw)
                _show_partial(response)
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
            if not worker.final_text and preview_text:
                worker.final_text = preview_text
            if session is not None:
                session.cancel()
            if accumulator is not None and worker.commit_info is None:
                try:
                    worker.commit_info = accumulator.finalize(
                        final_text=worker.final_text,
                        auto_hard_enter=auto_hard_enter,
                    )
                except Exception:
                    pass
        finally:
            try:
                reader.close()
            except Exception:
                pass

    worker.thread = threading.Thread(target=_run, name="recordian-realtime-asr", daemon=True)
    worker.thread.start()
    return worker
