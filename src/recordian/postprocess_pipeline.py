from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import streaming_correction as _streaming_correction
from .audio import read_wav_mono_f32
from .hotword_corrector import correct_hotwords, lexicon_from_args
from .linux_commit import (
    CompositionRefusedError,
    open_composition_session,
    paste_to_enter_delay_seconds,
    resolve_streaming_committer,
    send_hard_enter,
)
from .providers import provider_supports_file_streaming, provider_supports_realtime
from .refine_capture import append_refine_sample, resolve_refine_capture_path
from .remote_paste.client import resolve_remote_paste_routing, send_remote_paste_from_args

EventCallback = Callable[[dict[str, object]], None]


def _auto_lexicon_source(
    *,
    refiner: Any | None,
    raw_text: str,
    final_text: str,
    hotword_corrected: bool,
) -> str:
    """Classify how the accepted text was produced, for lexicon learning.

    ``refined`` — an LLM/text refiner produced the final text.
    ``corrected`` — deterministic hotword correction changed the ASR text.
    ``asr`` — the raw ASR output was accepted unchanged.
    Only a real user confirmation (outside this pipeline) may label text
    ``user_confirmed``; the pipeline never claims that by itself.
    """
    if refiner is not None and final_text.strip() and final_text != raw_text:
        return "refined"
    if hotword_corrected:
        return "corrected"
    return "asr"


def _observe_auto_lexicon(
    auto_lexicon: Any,
    text: str,
    *,
    source: str,
) -> int | None:
    """Observe accepted text for lexicon learning, with mandatory provenance.

    The call is made exactly once and always carries ``source``. A TypeError
    raised by the collaborator (an incompatible legacy signature or an
    internal bug) must NOT trigger a second, source-less call: the legacy
    unlabeled API increments ``accept_count`` — the same counter that
    promotes terms into the auto hotword pool — so a fallback retry would
    feed machine text into the pool without provenance. Fail closed instead:
    skip learning for this utterance and let the caller log the failure.
    """
    learned = auto_lexicon.observe_accepted(text, source=source)
    return int(learned) if isinstance(learned, int) else learned


# Outcomes that forbid every fallback commit path: the streaming session is
# terminal and the write state is unknown or known-bad. "suppressed" means a
# composition-capable backend refused the session (user preedit / no focus /
# sensitive / lost reply) — plain CommitText or clipboard fallbacks would
# clobber the user's composition or write into a new focus. Anything not
# listed here (committed / released_for_refine / no_composition) may commit.
_FALLBACK_SUPPRESSING_OUTCOMES = frozenset({"stale", "uncertain", "cancelled", "suppressed"})


def _commit_suppresses_fallback(
    commit_info: Mapping[str, object] | None,
    *,
    composition_started: bool = False,
) -> bool:
    """True when a streaming session ended terminally and any fallback commit is unsafe.

    The decision is driven by the structured ``outcome`` field set by the
    worker/controller ("stale" / "uncertain" / "cancelled"): the toolkit may
    already have committed our preedit by itself (focus lost, reset, user
    typed), or the commit reply was lost after a possible write (timeout,
    transport error). Committing the same text again through any fallback
    path could write into the wrong window or duplicate the text.

    ``composition_started`` is the worker's irreversible "a preedit was once
    bound" marker: without an explicit outcome a dead composition session is
    treated as uncertain (fail closed). Legacy detail strings are honored
    only as a last resort for callers that predate the outcome field.
    """
    if isinstance(commit_info, Mapping) and bool(commit_info.get("committed", False)):
        return False
    if isinstance(commit_info, Mapping):
        outcome = str(commit_info.get("outcome", "") or "")
        if outcome:
            return outcome in _FALLBACK_SUPPRESSING_OUTCOMES
    if composition_started:
        return True
    detail = str(commit_info.get("detail", "")).lower() if isinstance(commit_info, Mapping) else ""
    return (
        "stale" in detail
        or "focus_lost" in detail
        or "user_typed" in detail
        or "timeout_suppressed" in detail
    )



def _extract_refine_postprocess_rule(prompt_template: str | None) -> tuple[str, str | None]:
    if not prompt_template:
        return "none", prompt_template
    lines = prompt_template.splitlines()
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("@postprocess:"):
            rule = stripped.split(":", 1)[1].strip().lower() or "none"
            if rule not in {"none", "repeat-lite", "zh-stutter-lite"}:
                rule = "none"
            lines.pop(idx)
            cleaned_prompt = "\n".join(lines).strip()
            return rule, (cleaned_prompt or None)
        break
    return "none", prompt_template


def _cleanup_stutter_text(text: str) -> str:
    """Conservative deterministic cleanup for common stutter repetitions."""
    import re

    if not text:
        return ""

    cleaned = str(text)
    clause_boundary = r"(?:(?<=^)|(?<=[，。！？；：、,.!?\s]))"
    following_content = r"(?=[\u4e00-\u9fffA-Za-z0-9])"

    common_words = [
        "这个",
        "那个",
        "就是",
        "然后",
        "我们",
        "你们",
        "他们",
        "她们",
        "它们",
    ]
    for token in common_words:
        cleaned = re.sub(rf"(?:{re.escape(token)})(?:\s*{re.escape(token)})+", token, cleaned)

    stutter_chars = "我你他她它这那要就先再嗯啊呃额诶欸"
    cleaned = re.sub(
        rf"{clause_boundary}([{stutter_chars}])(?:\s*\1)+{following_content}",
        r"\1",
        cleaned,
    )

    filler_tokens = [
        "然后呢",
        "然后",
        "同时呢",
        "同时",
        "这个呢",
        "这个",
        "那个呢",
        "那个",
        "就是",
        "嗯",
        "啊",
        "呃",
        "额",
        "诶",
        "欸",
        "a",
        "A",
    ]
    filler_group = "|".join(re.escape(token) for token in filler_tokens)
    cleaned = re.sub(
        rf"(?:(?<=^)|(?<=[。！？；]))(?:\s*(?:{filler_group})[，、,\s]*){{2,6}}(?=[\u4e00-\u9fffA-Za-z0-9])",
        "",
        cleaned,
    )
    cleaned = re.sub(
        rf"([，,]\s*)(?:{filler_group})(?=[，,])",
        r"\1",
        cleaned,
    )
    cleaned = re.sub(r"[，,]\s*[，,]+", "，", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    return cleaned


def _cleanup_repeat_lite_text(text: str) -> str:
    import re

    if not text:
        return ""
    cleaned = str(text)
    cleaned = re.sub(
        r"(?i)\b([a-z][a-z0-9'_-]{0,31})(?:\s+\1){1,}\b",
        r"\1",
        cleaned,
    )
    return cleaned


def _apply_refine_postprocess(text: str, *, rule: str) -> str:
    normalized_rule = str(rule or "none").strip().lower()
    if normalized_rule == "zh-stutter-lite":
        return _cleanup_stutter_text(text)
    if normalized_rule == "repeat-lite":
        return _cleanup_repeat_lite_text(text)
    return text


_REFINE_PROTECTED_STOPWORDS = {
    "这个",
    "那个",
    "就是",
    "然后",
    "我们",
    "你们",
    "他们",
    "她们",
    "它们",
    "可以",
    "一下",
    "还有",
    "是不是",
    "现在",
    "时候",
    "what",
    "this",
    "that",
    "and",
    "then",
}


def _text_contains_term(text: str, term: str) -> bool:
    source = str(text or "")
    token = str(term or "").strip()
    if not source or not token:
        return False
    if token.isascii():
        return token.lower() in source.lower()
    return token in source


def _select_refine_protected_terms(text: str, hotwords: list[str], *, max_terms: int = 12) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw in hotwords:
        token = str(raw).strip()
        if not token:
            continue
        key = token.lower() if token.isascii() else token
        if key in seen:
            continue
        seen.add(key)
        if len(token) < 2 or len(token) > 32:
            continue
        if key in _REFINE_PROTECTED_STOPWORDS:
            continue
        if not _text_contains_term(text, token):
            continue
        selected.append(token)
        if len(selected) >= max(0, int(max_terms)):
            break
    return selected


def _build_refine_prompt_with_protected_terms(prompt_template: str | None, protected_terms: list[str]) -> str | None:
    if not prompt_template:
        return prompt_template
    terms = [str(t).strip() for t in protected_terms if str(t).strip()]
    if not terms:
        return prompt_template
    term_line = "、".join(terms)
    guard = (
        "附加约束（必须遵守）：下列词语如果在原文中出现，输出时必须原样保留，"
        "不得改写、同义替换或删除："
        f"{term_line}\n"
    )
    return guard + "\n" + prompt_template


def _select_refine_correction_terms(text: str, hotwords: list[str], *, max_terms: int = 24) -> list[str]:
    """Hotwords NOT present in *text* — candidates the ASR may have misrecognized.

    These are handed to the refiner as correction targets so it can rewrite
    homophone / near-spelling variants (Cloud → Claude, SPARK → SPARC) back to
    the canonical hotword form.
    """
    selected: list[str] = []
    seen: set[str] = set()
    for raw in hotwords:
        token = str(raw).strip()
        if not token:
            continue
        key = token.lower() if token.isascii() else token
        if key in seen:
            continue
        seen.add(key)
        if len(token) < 2 or len(token) > 32:
            continue
        if key in _REFINE_PROTECTED_STOPWORDS:
            continue
        if _text_contains_term(text, token):
            continue
        selected.append(token)
        if len(selected) >= max(0, int(max_terms)):
            break
    return selected


def _build_refine_prompt_with_guards(
    prompt_template: str | None,
    protected_terms: list[str],
    correction_terms: list[str],
) -> str | None:
    """Build refine prompt with hotword guards: correction targets + protection."""
    if not prompt_template:
        return prompt_template
    corrections = [str(t).strip() for t in correction_terms if str(t).strip()]
    if not corrections:
        return _build_refine_prompt_with_protected_terms(prompt_template, protected_terms)

    sections: list[str] = []
    correction_line = "、".join(corrections)
    sections.append(
        "1. 标准术语表："
        f"{correction_line}\n"
        "   原文中如果出现与术语表里某个词发音相同/相近（同音字、近音字）或拼写相近"
        "（大小写、空格、连字符差异，或个别字母错误）的片段，必须改写为术语表中的标准形式。"
        "例如原文是 Cloud 而术语表有 Claude 时，输出 Claude。"
        "注意：只有当片段在语境中确实指该术语时才改写，普通英语单词不要误改。"
    )
    protected = [str(t).strip() for t in protected_terms if str(t).strip()]
    if protected:
        term_line = "、".join(protected)
        sections.append(
            "2. 下列词语已在原文中出现，输出时必须原样保留，不得改写、同义替换或删除："
            f"{term_line}"
        )
    guard = "附加约束（必须遵守）：\n" + "\n".join(sections) + "\n"
    return guard + "\n" + prompt_template


def _preview_text(text: str, max_len: int = 48) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_auto_hard_enter(args: argparse.Namespace) -> bool:
    default = bool(getattr(args, "auto_hard_enter", False))
    raw_path = str(getattr(args, "config_path", "")).strip()
    if not raw_path:
        return default
    try:
        path = Path(raw_path).expanduser()
        if not path.exists():
            return default
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return _coerce_bool(payload.get("auto_hard_enter", default), default=default)
    except Exception:
        return default
    return default


def _commit_text(committer: Any, text: str, *, auto_hard_enter: bool = False) -> dict[str, object]:
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


def _merge_stream_text(prev: str, current: str) -> str:
    if not prev:
        return current
    if current.startswith(prev):
        return current
    if prev.endswith(current):
        return prev
    return prev + current


def _stream_display_delta(prev_display: str, next_display: str) -> tuple[str, str]:
    if next_display == prev_display:
        return prev_display, ""
    if next_display.startswith(prev_display):
        return next_display, next_display[len(prev_display):]
    if prev_display.startswith(next_display):
        return prev_display, ""
    return prev_display, ""


@dataclass(slots=True)
class _StreamingCommitAccumulator:
    committer: Any
    accumulated_text: str = ""
    chunk_count: int = 0
    any_committed: bool = False
    last_backend: str = ""
    last_result: Any | None = None
    error: str = ""
    pending_text: str = ""
    last_flush_started_at: float = 0.0

    def _flush_policy(self) -> tuple[int, float]:
        backend = str(getattr(self.committer, "backend_name", "")).strip().lower()
        if backend == "xdotool-clipboard":
            return 12, 0.35
        return 1, 0.0

    def _flush_pending(self) -> None:
        if not self.pending_text or self.error:
            return
        token = self.pending_text
        self.pending_text = ""
        try:
            result = self.committer.commit(token)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return
        self.chunk_count += 1
        self.last_result = result
        self.last_backend = str(getattr(result, "backend", "") or getattr(self.committer, "backend_name", "unknown"))
        if bool(getattr(result, "committed", False)):
            self.any_committed = True
        self.last_flush_started_at = time.monotonic()

    def append_chunk(self, chunk: str) -> None:
        token = str(chunk)
        if token == "":
            return
        self.accumulated_text += token
        if self.error:
            return
        self.pending_text += token
        if self.last_flush_started_at <= 0.0:
            self.last_flush_started_at = time.monotonic()
        min_chars, max_delay_s = self._flush_policy()
        now = time.monotonic()
        if len(self.pending_text) >= min_chars or (max_delay_s > 0.0 and now - self.last_flush_started_at >= max_delay_s):
            self._flush_pending()

    def finalize(self, *, final_text: str, auto_hard_enter: bool) -> dict[str, object]:
        self._flush_pending()
        if self.error and not self.any_committed and final_text.strip():
            return _commit_text(self.committer, final_text, auto_hard_enter=auto_hard_enter)

        backend = self.last_backend or getattr(self.committer, "backend_name", "unknown")
        details: list[str] = []
        if self.chunk_count:
            details.append(f"streaming_chunks:{self.chunk_count}")
        if self.error:
            details.append(f"streaming_error:{self.error}")
        if auto_hard_enter and self.any_committed and self.last_result is not None:
            enter_delay_s = paste_to_enter_delay_seconds(self.last_result)
            if enter_delay_s > 0.0:
                time.sleep(enter_delay_s)
            enter_result = send_hard_enter(self.committer)
            enter_detail = str(getattr(enter_result, "detail", "")).strip()
            if enter_detail:
                details.append(enter_detail)
        detail = ";".join(part for part in details if part) or "streaming_complete"
        return {
            "backend": backend,
            "committed": self.any_committed,
            "detail": detail,
        }


def _remote_only_commit_info(remote_result: Mapping[str, Any]) -> dict[str, object]:
    detail = str(remote_result.get("detail", "")).strip() or "remote_paste_failed"
    return {
        "backend": "remote-paste",
        "committed": bool(remote_result.get("sent", False)),
        "detail": detail,
    }


def _apply_target_window(committer: Any, state: Mapping[str, object]) -> None:
    """Set target window on committer when supported."""
    wid = state.get("target_window_id")
    if hasattr(committer, "target_window_id"):
        committer.target_window_id = wid if isinstance(wid, int) else None
    nested = getattr(committer, "committers", None)
    if isinstance(nested, list):
        for entry in nested:
            if isinstance(entry, tuple) and entry:
                _apply_target_window(entry[0], state)


def _should_skip_owner_gated_asr(
    *,
    owner_filter_enabled: bool,
    owner_seen: bool,
    owner_last_score: float,
) -> bool:
    return bool(owner_filter_enabled) and not bool(owner_seen) and float(owner_last_score) >= 0.0


def _emit_result(
    on_result: EventCallback,
    *,
    audio_path: Path,
    record_backend: str,
    record_latency_ms: float,
    transcribe_latency_ms: float,
    refine_latency_ms: float,
    text: str,
    detected_language: str,
    asr_provider: str,
    asr_path: str,
    asr_capabilities: str,
    commit: dict[str, object],
) -> None:
    on_result(
        {
            "event": "result",
            "result": {
                "audio_path": str(audio_path),
                "record_backend": record_backend,
                "duration_s": record_latency_ms / 1000.0,
                "record_latency_ms": record_latency_ms,
                "transcribe_latency_ms": transcribe_latency_ms,
                "refine_latency_ms": refine_latency_ms,
                "text": text,
                "detected_language": detected_language,
                "asr_provider": asr_provider,
                "asr_path": asr_path,
                "asr_capabilities": asr_capabilities,
                "commit": commit,
            },
        }
    )


def _describe_asr_provider(provider: object) -> str:
    name = str(getattr(provider, "provider_name", "")).strip()
    if name:
        return name
    return type(provider).__name__


def _describe_asr_capabilities(provider: object) -> str:
    capabilities = getattr(provider, "capabilities", None)
    labels: list[str] = []
    if bool(getattr(capabilities, "supports_hotwords", False)):
        labels.append("hotwords")
    if bool(getattr(capabilities, "supports_context", False)):
        labels.append("context")
    if bool(getattr(capabilities, "supports_language_hint", False)):
        labels.append("language_hint")
    if provider_supports_file_streaming(provider):
        labels.append("file_streaming")
    if provider_supports_realtime(provider):
        labels.append("realtime")
    if not labels:
        return "basic"
    seen: set[str] = set()
    deduped: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        deduped.append(label)
    return ",".join(deduped)


def _capture_refine_sample(
    *,
    args: argparse.Namespace,
    audio_path: Path,
    record_backend: str,
    raw_text: str,
    final_text: str,
    refiner: Any | None,
    transcribe_latency_ms: float,
    refine_latency_ms: float,
    commit_info: dict[str, object],
    on_state: EventCallback,
) -> None:
    if not bool(getattr(args, "capture_refine_samples", False)):
        return
    if not str(raw_text).strip() and not str(final_text).strip():
        return

    output_path = resolve_refine_capture_path(
        getattr(args, "capture_refine_samples_path", "")
    )
    refine_enabled = bool(getattr(args, "enable_text_refine", False))
    refiner_ready = refiner is not None
    refine_skipped = bool(getattr(refiner, "last_refine_skipped", False)) if refiner is not None else False
    append_refine_sample(
        output_path=output_path,
        audio_path=audio_path,
        raw_asr_text=raw_text,
        final_text=final_text,
        refine_applied=refiner_ready and bool(str(raw_text).strip()) and not refine_skipped,
        refine_changed=str(raw_text).strip() != str(final_text).strip(),
        refine_preset=str(getattr(args, "refine_preset", "default")).strip() or "default",
        refine_provider=str(getattr(args, "refine_provider", "")).strip(),
        refine_model=str(getattr(refiner, "model_name", "") or getattr(refiner, "model", "")).strip(),
        refine_enabled=refine_enabled,
        refiner_ready=refiner_ready,
        record_backend=record_backend,
        transcribe_latency_ms=transcribe_latency_ms,
        refine_latency_ms=refine_latency_ms,
        commit_info=commit_info,
    )
    if getattr(args, "debug_diagnostics", False):
        on_state({"event": "log", "message": f"diag refine_sample_captured={output_path}"})


def _resolve_refine_llm_max_len(args: argparse.Namespace) -> int:
    """Chars threshold above which the LLM rewrite is skipped (0 = always refine)."""
    try:
        return max(0, int(getattr(args, "refine_max_len_llm", 0)))
    except Exception:
        return 0


def _should_skip_llm_refine(args: argparse.Namespace, text: str) -> bool:
    """Long-text fast path: skip the whole-document LLM rewrite and only clean locally.

    Refinement regenerates roughly the full input length, so cost grows linearly with
    the utterance and the single cloud request can exceed its HTTP timeout. Above the
    configured threshold the deterministic postprocess cleanup (de-dup / filler words)
    is still applied without paying LLM time.
    """
    threshold = _resolve_refine_llm_max_len(args)
    if threshold <= 0:
        return False
    return len(str(text or "")) > threshold


def _sync_refiner_preset(args: argparse.Namespace, refiner: Any, on_state: EventCallback) -> None:
    if not hasattr(refiner, "update_preset"):
        return
    try:
        config_path = getattr(args, "config_path", None)
        if config_path and Path(config_path).exists():
            current_config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            new_preset = current_config.get("refine_preset", "default")
            old_preset = getattr(args, "refine_preset", "default")
            if new_preset != old_preset:
                refiner.update_preset(new_preset)
                args.refine_preset = new_preset
                on_state({"event": "log", "message": f"已切换到 {new_preset} preset"})
    except Exception as exc:  # noqa: BLE001
        on_state({"event": "log", "message": f"preset 热切换失败: {exc}"})


def _run_refinement(
    *,
    args: argparse.Namespace,
    refiner: Any,
    text: str,
    effective_hotwords: list[str],
    refine_postprocess_rule: str,
    on_state: EventCallback,
) -> tuple[str, float]:
    threshold = _resolve_refine_llm_max_len(args)
    if threshold > 0 and len(text) > threshold:
        refiner.last_refine_skipped = True
        on_state(
            {
                "event": "log",
                "message": (
                    f"refine_skip_long_text: len={len(text)} max_len_llm={threshold}"
                    f" rule={refine_postprocess_rule or 'none'}"
                    " — 长文跳过 LLM 精炼，仅做确定性清洗"
                ),
            }
        )
        cleaned = _apply_refine_postprocess(text, rule=refine_postprocess_rule)
        return cleaned, 0.0

    refiner.last_refine_skipped = False
    _sync_refiner_preset(args, refiner, on_state)

    t1 = time.perf_counter()
    base_prompt_template = getattr(refiner, "prompt_template", None)
    protected_terms = _select_refine_protected_terms(text, effective_hotwords)
    correction_terms = _select_refine_correction_terms(text, effective_hotwords)
    prompt_with_guards = _build_refine_prompt_with_guards(base_prompt_template, protected_terms, correction_terms)
    if prompt_with_guards != base_prompt_template:
        refiner.prompt_template = prompt_with_guards
    if args.debug_diagnostics and (protected_terms or correction_terms):
        on_state(
            {
                "event": "log",
                "message": (
                    f"diag refine_protected_terms={protected_terms}"
                    f" refine_correction_terms={correction_terms}"
                ),
            }
        )

    on_state({"event": "log", "message": f"ASR 原始输出: {text}"})

    refined_text = ""
    try:
        if getattr(args, "enable_streaming_refine", False):
            for chunk in refiner.refine_stream(text):
                refined_text += chunk
                on_state(
                    {
                        "event": "refine_stream_chunk",
                        "chunk": chunk,
                        "accumulated": refined_text,
                    }
                )
        else:
            refined_text = refiner.refine(text)
    except Exception as exc:  # noqa: BLE001
        on_state(
            {
                "event": "log",
                "message": (
                    f"text_refine_failed: {type(exc).__name__}: {exc}"
                    " — 精炼失败，将按 ASR 原文提交"
                ),
            }
        )
    finally:
        if prompt_with_guards != base_prompt_template:
            refiner.prompt_template = base_prompt_template

    refine_latency_ms = (time.perf_counter() - t1) * 1000
    if refined_text.strip():
        on_state({"event": "log", "message": f"精炼后输出: {refined_text}"})
        text = refined_text
    text = _apply_refine_postprocess(text, rule=refine_postprocess_rule)
    return text, refine_latency_ms


def _streaming_commit_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "enable_streaming_commit", False))


def _apply_hotword_correction(
    text: str,
    *,
    args: argparse.Namespace,
    hotwords: list[str],
    on_state: EventCallback,
    log_changes: bool = True,
) -> str:
    if not text.strip():
        return text
    if not _coerce_bool(getattr(args, "enable_hotword_correction", True), default=True):
        return text
    _hotwords, replacements = lexicon_from_args(args)
    effective = list(hotwords or _hotwords)
    if not effective and not replacements:
        return text
    try:
        max_edits = max(0, int(getattr(args, "hotword_correction_edits", 1)))
    except Exception:
        max_edits = 1
    corrected_text, hotword_changes = correct_hotwords(
        text,
        effective,
        max_ascii_edits=max_edits,
        replacements=replacements,
    )
    if hotword_changes:
        if log_changes:
            preview = "; ".join(f"{src}→{dst}" for src, dst in hotword_changes[:8])
            on_state({"event": "log", "message": f"hotword_corrected: {preview}"})
        return corrected_text
    return text


def _apply_final_semif(
    text: str,
    *,
    args: argparse.Namespace,
    hotwords: list[str],
    on_state: EventCallback,
) -> str:
    """One bounded end-of-sentence SemIf judgment for non-streaming finals.

    The realtime worker already applies this to its own final text
    (``prefetched_semif_applied``); this covers the remaining real entry
    points: the full-audio fallback after a preview-only worker failure and
    plain oneshot dictation. Deterministic snapshot first, then at most one
    provider budget from ``corrector_from_args`` (SemIf stays within 0.35 s;
    official Jev uses its own budget). Preview text does not wait here.
    """
    if not text.strip():
        return text
    if not bool(getattr(args, "enable_semif_correction", False)):
        return text
    effective = list(hotwords)
    lexicon, pairs = lexicon_from_args(args)
    if not effective:
        effective = list(lexicon)
    for src, dst in pairs or []:
        effective.append(f"{src}→{dst}")
    seen: set[str] = set()
    merged: list[str] = []
    for t in effective:
        if t in seen:
            continue
        seen.add(t)
        merged.append(t)
    corrector = _streaming_correction.corrector_from_args(args, merged)
    try:
        return corrector.finish(text)
    except Exception as exc:  # noqa: BLE001
        on_state(
            {
                "event": "log",
                "message": f"final_semif_correction_failed: {type(exc).__name__}: {exc}",
            }
        )
        return text
    finally:
        corrector.close()


def _open_stream_composition(context: PostprocessPipelineContext) -> Any:
    """Try to open a preedit composition session for file-streaming commit.

    Returns the session, or None when the backend has no composition API
    (legacy one-shot commit semantics; transient non-refusal failures also
    degrade to preview-only streaming). Raises CompositionRefusedError when
    a composition-capable backend refused (user preedit / no focus /
    sensitive / table full / lost reply): that must suppress the whole
    utterance — no streaming writes, no plain CommitText fallback.
    """
    streaming_committer = resolve_streaming_committer(context.committer)
    try:
        return open_composition_session(streaming_committer)
    except CompositionRefusedError:
        raise
    except Exception as exc:  # noqa: BLE001
        context.on_state(
            {
                "event": "log",
                "message": (
                    f"stream_composition_unavailable: {type(exc).__name__}: {exc}"
                    " — preview-only streaming, single final commit"
                ),
            }
        )
        return None


def _composition_refused_info(exc: CompositionRefusedError) -> dict[str, object]:
    return {
        "backend": "fcitx",
        "committed": False,
        "detail": f"composition_refused:{exc}",
        "outcome": "suppressed",
    }


def _session_preedit(session: Any, text: str, context: PostprocessPipelineContext) -> bool:
    """Push a preedit update; return False once the session went stale."""
    if session is None or not getattr(session, "active", True):
        return True
    result = session.update_preedit(text)
    detail = str(getattr(result, "detail", ""))
    if not bool(getattr(result, "committed", False)):
        # Fail closed on every failure: FcitxStreamingSession marks any
        # failed update stale (busctl strips DBus error names, so only a
        # confirmed reply proves the bound context is still live). Stop
        # streaming and never fall back to a commit elsewhere.
        context.on_state(
            {
                "event": "log",
                "message": (
                    "stream_session_stale: preedit update failed "
                    f"(outcome={getattr(result, 'outcome', '')} detail={detail}) "
                    "— no fallback commit"
                ),
            }
        )
        return False
    return True


def _session_final_commit(
    session: Any,
    streaming_committer: Any,
    final_text: str,
    *,
    auto_hard_enter: bool,
) -> dict[str, object]:
    """Commit the final text exactly once through the composition session.

    Legacy path (no session): single one-shot commit, no synthetic typing.
    The returned dict always carries a structured ``outcome``; a failed
    session commit is terminal ("stale" / "uncertain") and the caller must
    not fall back to any other commit path.
    """
    if session is not None:
        if not final_text:
            session.cancel()
            return {
                "backend": "fcitx",
                "committed": False,
                "detail": "stream_empty_final",
                "outcome": "cancelled",
            }
        result = session.commit(final_text)
        committed = bool(getattr(result, "committed", False))
        detail = str(getattr(result, "detail", ""))
        outcome = str(getattr(result, "outcome", "") or "") or (
            "committed" if committed else "uncertain"
        )
        if committed and auto_hard_enter:
            enter_delay_s = paste_to_enter_delay_seconds(result)
            if enter_delay_s > 0.0:
                time.sleep(enter_delay_s)
            enter_detail = str(getattr(send_hard_enter(streaming_committer), "detail", "")).strip()
            if enter_detail:
                detail = f"{detail};{enter_detail}"
        return {"backend": "fcitx", "committed": committed, "detail": detail, "outcome": outcome}
    commit_info = _commit_text(streaming_committer, final_text, auto_hard_enter=auto_hard_enter)
    detail = str(commit_info.get("detail", "")).strip()
    commit_info["detail"] = f"{detail};stream_preview_only" if detail else "stream_preview_only"
    commit_info["outcome"] = "no_composition"
    return commit_info


def _run_asr_streaming_commit(
    *,
    context: PostprocessPipelineContext,
    effective_hotwords: list[str],
    auto_hard_enter: bool,
) -> tuple[str, float, dict[str, object]]:
    if not provider_supports_file_streaming(context.provider):
        raise NotImplementedError("asr_stream_unsupported")

    streaming_committer = resolve_streaming_committer(context.committer)
    try:
        session = _open_stream_composition(context)
    except CompositionRefusedError as exc:
        # Backend supports composition but refused (user preedit / no focus /
        # sensitive / lost reply): zero writes, zero fallback for this pass.
        context.on_state(
            {
                "event": "log",
                "message": (
                    f"stream_composition_refused: {exc} — "
                    "本次不提交不退格，不做任何本地回退"
                ),
            }
        )
        return "", 0.0, _composition_refused_info(exc)
    raw_streamed_text = ""
    display_text = ""
    t0 = time.perf_counter()
    try:
        for chunk in context.provider.transcribe_file_stream(context.audio_path, hotwords=effective_hotwords):
            raw_streamed_text = _merge_stream_text(raw_streamed_text, str(chunk))
            normalized_text = _apply_hotword_correction(
                context.normalize_final_text(raw_streamed_text),
                args=context.args,
                hotwords=effective_hotwords,
                on_state=context.on_state,
                log_changes=False,
            )
            if normalized_text == display_text:
                continue
            display_text = normalized_text
            context.on_state(
                {
                    "event": "stream_partial",
                    "chunk": "",
                    "text": display_text,
                }
            )
            if not _session_preedit(session, display_text, context):
                # Focus lost / user typed: stop streaming, never fall back to a
                # commit that could write into the wrong window.
                transcribe_latency_ms = (time.perf_counter() - t0) * 1000
                return display_text, transcribe_latency_ms, {
                    "backend": "fcitx",
                    "committed": False,
                    "detail": "stream_session_stale_focus_lost",
                    "outcome": "stale",
                }
    except Exception:
        if session is not None:
            # A composition session was already bound: the failure is
            # terminal, never a fallback candidate. Clear our own preedit.
            try:
                session.cancel()
            except Exception:  # noqa: BLE001
                pass
            transcribe_latency_ms = (time.perf_counter() - t0) * 1000
            return display_text, transcribe_latency_ms, {
                "backend": "fcitx",
                "committed": False,
                "detail": "stream_session_failed_no_fallback",
                "outcome": "cancelled",
            }
        raise
    transcribe_latency_ms = (time.perf_counter() - t0) * 1000
    final_text = display_text.strip()
    if session is not None and not final_text:
        # Empty stream with ZERO preedit writes: nothing of ours is on the
        # screen, so the one-shot full-audio transcription is a legitimate
        # retry (a provider streaming glitch must not drop the utterance).
        # The retry commits through the SAME still-bound composition token:
        # if the context lost focus while transcribe_file ran, CommitSession
        # fails stale and no fallback path may write into the new focus.
        # Failed or partial streams never reach here — they are terminal.
        try:
            oneshot = context.provider.transcribe_file(
                context.audio_path, hotwords=effective_hotwords
            )
        except Exception:
            try:
                session.cancel()
            except Exception:  # noqa: BLE001
                pass
            transcribe_latency_ms = (time.perf_counter() - t0) * 1000
            return "", transcribe_latency_ms, {
                "backend": "fcitx",
                "committed": False,
                "detail": "stream_oneshot_retry_failed_no_fallback",
                "outcome": "cancelled",
            }
        transcribe_latency_ms = (time.perf_counter() - t0) * 1000
        retry_text = context.normalize_final_text(str(getattr(oneshot, "text", "")))
        if retry_text.strip():
            retry_text = _apply_hotword_correction(
                retry_text,
                args=context.args,
                hotwords=effective_hotwords,
                on_state=context.on_state,
            )
            retry_text = _apply_final_semif(
                retry_text,
                args=context.args,
                hotwords=effective_hotwords,
                on_state=context.on_state,
            )
            context.on_state(
                {
                    "event": "log",
                    "message": (
                        "asr_stream_commit_fallback: empty_stream_retry_same_session "
                        f"provider={_describe_asr_provider(context.provider)}"
                    ),
                }
            )
            final_text = retry_text.strip()
    commit_info = _session_final_commit(
        session,
        streaming_committer,
        final_text,
        auto_hard_enter=auto_hard_enter,
    )
    return final_text, transcribe_latency_ms, commit_info


def _run_refinement_streaming_commit(
    *,
    context: PostprocessPipelineContext,
    text: str,
    effective_hotwords: list[str],
    auto_hard_enter: bool,
) -> tuple[str, float, dict[str, object]]:
    refiner = context.refiner
    assert refiner is not None
    # The pipeline entry already excluded long texts
    # (_should_skip_llm_refine), so the LLM rewrite runs: keep the
    # diagnostic marker consistent with _run_refinement.
    refiner.last_refine_skipped = False
    _sync_refiner_preset(context.args, refiner, context.on_state)

    base_prompt_template = getattr(refiner, "prompt_template", None)
    protected_terms = _select_refine_protected_terms(text, effective_hotwords)
    correction_terms = _select_refine_correction_terms(text, effective_hotwords)
    prompt_with_guards = _build_refine_prompt_with_guards(base_prompt_template, protected_terms, correction_terms)
    if prompt_with_guards != base_prompt_template:
        refiner.prompt_template = prompt_with_guards
    if context.args.debug_diagnostics and (protected_terms or correction_terms):
        context.on_state(
            {
                "event": "log",
                "message": (
                    f"diag refine_protected_terms={protected_terms}"
                    f" refine_correction_terms={correction_terms}"
                ),
            }
        )

    context.on_state({"event": "log", "message": f"ASR 原始输出: {text}"})
    streaming_committer = resolve_streaming_committer(context.committer)
    try:
        session = _open_stream_composition(context)
    except CompositionRefusedError as exc:
        context.on_state(
            {
                "event": "log",
                "message": (
                    f"stream_composition_refused: {exc} — "
                    "本次不提交不退格，不做任何本地回退"
                ),
            }
        )
        return text, 0.0, _composition_refused_info(exc)
    refined_text = ""
    stale = False
    failed_terminal = False
    t1 = time.perf_counter()
    try:
        for chunk in refiner.refine_stream(text):
            token = str(chunk)
            if not token:
                continue
            refined_text += token
            context.on_state(
                {
                    "event": "refine_stream_chunk",
                    "chunk": token,
                    "accumulated": refined_text,
                }
            )
            if not _session_preedit(session, refined_text, context):
                stale = True
                break
    except Exception:
        if session is not None:
            # Composition already bound: terminal, no fallback. Clear our
            # own preedit so nothing lingers.
            try:
                session.cancel()
            except Exception:  # noqa: BLE001
                pass
            failed_terminal = True
        else:
            raise
    finally:
        if prompt_with_guards != base_prompt_template:
            refiner.prompt_template = base_prompt_template

    refine_latency_ms = (time.perf_counter() - t1) * 1000
    final_text = refined_text.strip()
    if stale:
        return final_text, refine_latency_ms, {
            "backend": "fcitx",
            "committed": False,
            "detail": "stream_session_stale_focus_lost",
            "outcome": "stale",
        }
    if failed_terminal:
        return final_text, refine_latency_ms, {
            "backend": "fcitx",
            "committed": False,
            "detail": "refine_stream_failed_no_fallback",
            "outcome": "cancelled",
        }
    if final_text:
        context.on_state({"event": "log", "message": f"精炼后输出: {final_text}"})
    # Streaming refine finalizes through the SAME deterministic cleanup as
    # the non-streaming refiner: the preset's @postprocess rule
    # (zh-stutter-lite / repeat-lite) must not silently stop applying just
    # because the chunks streamed.
    final_text = _apply_refine_postprocess(final_text, rule=context.refine_postprocess_rule).strip()
    commit_info = _session_final_commit(
        session,
        streaming_committer,
        final_text,
        auto_hard_enter=auto_hard_enter,
    )
    return final_text, refine_latency_ms, commit_info


@dataclass(slots=True)
class PostprocessPipelineContext:
    args: argparse.Namespace
    audio_path: Path
    record_backend: str
    record_latency_ms: float
    owner_filter_enabled: bool
    owner_seen: bool
    owner_last_score: float
    state: Mapping[str, object]
    provider: Any
    refiner: Any | None
    committer: Any
    auto_lexicon: Any | None
    refine_postprocess_rule: str
    normalize_final_text: Callable[[str], str]
    resolve_hotwords: Callable[[], list[str]]
    on_state: EventCallback
    on_result: EventCallback
    on_error: EventCallback
    prefetched_asr_text: str = ""
    prefetched_detected_language: str = ""
    prefetched_transcribe_latency_ms: float = 0.0
    prefetched_commit_info: dict[str, object] | None = None
    # Structured terminal outcome of the realtime worker's composition
    # session ("committed" / "released_for_refine" / "stale" / "uncertain" /
    # "cancelled" / "no_composition"), mirrored inside prefetched_commit_info.
    prefetched_outcome: str = ""
    # Irreversible worker marker: a preedit was bound to the focused context
    # at some point. Unknown outcomes are treated as uncertain (no fallback).
    prefetched_composition_started: bool = False
    # Live composition session carried from the worker when the outcome is
    # "released_for_refine": refinement commits through this exact token.
    composition_session: Any = None
    # True when the realtime worker already ran the end-of-sentence SemIf
    # judgment on the prefetched text (do not judge it twice).
    prefetched_semif_applied: bool = False


def run_postprocess_pipeline(context: PostprocessPipelineContext) -> None:
    try:
        if _should_skip_owner_gated_asr(
            owner_filter_enabled=context.owner_filter_enabled,
            owner_seen=context.owner_seen,
            owner_last_score=context.owner_last_score,
        ):
            context.on_state(
                {
                    "event": "log",
                    "message": (
                        "voice_owner_gate_rejected: "
                        f"owner_seen={context.owner_seen} last_score={context.owner_last_score:.3f}"
                    ),
                }
            )
            _emit_result(
                context.on_result,
                audio_path=context.audio_path,
                record_backend=context.record_backend,
                record_latency_ms=context.record_latency_ms,
                transcribe_latency_ms=0.0,
                refine_latency_ms=0.0,
                text="",
                detected_language="",
                asr_provider=_describe_asr_provider(context.provider),
                asr_path="owner_gate_skipped",
                asr_capabilities=_describe_asr_capabilities(context.provider),
                commit={
                    "backend": "none",
                    "committed": False,
                    "detail": "owner_gate_rejected_no_owner_speech",
                },
            )
            return
        if context.owner_filter_enabled and not context.owner_seen and context.owner_last_score < 0.0:
            context.on_state(
                {
                    "event": "log",
                    "message": "voice_owner_gate_inconclusive: fallback_to_asr",
                }
            )

        try:
            import numpy as np

            samples = read_wav_mono_f32(context.audio_path)
            rms = float(np.sqrt(np.mean(samples ** 2)))
            if rms < 0.003:
                context.on_state({"event": "log", "message": f"静音跳过 ASR (rms={rms:.4f})"})
                _emit_result(
                    context.on_result,
                    audio_path=context.audio_path,
                    record_backend=context.record_backend,
                    record_latency_ms=0.0,
                    transcribe_latency_ms=0.0,
                    refine_latency_ms=0.0,
                    text="",
                    detected_language="",
                    asr_provider=_describe_asr_provider(context.provider),
                    asr_path="silence_skipped",
                    asr_capabilities=_describe_asr_capabilities(context.provider),
                    commit={"backend": "none", "committed": False, "detail": "silence_skipped"},
                )
                return
        except Exception:
            pass

        auto_hard_enter = _resolve_auto_hard_enter(context.args)
        routing = resolve_remote_paste_routing(context.args)
        t0 = time.perf_counter()
        effective_hotwords = context.resolve_hotwords()
        raw_text = ""
        text = ""
        detected_language = ""
        transcribe_latency_ms = 0.0
        refine_latency_ms = 0.0
        commit_info: dict[str, object]
        asr_path = "oneshot"
        asr_provider = _describe_asr_provider(context.provider)
        asr_capabilities = _describe_asr_capabilities(context.provider)

        if routing.commit_local:
            _apply_target_window(context.committer, context.state)

        streamed_commit = False
        hotword_corrected = False
        realtime_already_committed = bool(
            isinstance(context.prefetched_commit_info, dict)
            and context.prefetched_commit_info.get("committed")
        )
        # A realtime session may already have typed the utterance. Do not run a
        # second file-stream commit on key release.
        realtime_session_ran = isinstance(context.prefetched_commit_info, dict)
        if (
            routing.commit_local
            and _streaming_commit_enabled(context.args)
            and context.refiner is None
            and not realtime_already_committed
            and not realtime_session_ran
        ):
            try:
                text, transcribe_latency_ms, commit_info = _run_asr_streaming_commit(
                    context=context,
                    effective_hotwords=effective_hotwords,
                    auto_hard_enter=auto_hard_enter,
                )
                if text.strip() and bool(commit_info.get("committed")):
                    raw_text = text
                    streamed_commit = True
                    asr_path = "streaming_commit"
                elif _commit_suppresses_fallback(commit_info):
                    # Terminal streaming outcome: committing again could
                    # duplicate text or write into the wrong window.
                    raw_text = text
                    streamed_commit = True
                    if str(commit_info.get("outcome", "")) == "suppressed":
                        asr_path = "stream_composition_refused_suppressed"
                        context.on_state(
                            {
                                "event": "log",
                                "message": (
                                    "stream_commit_suppressed: composition session "
                                    "refused (user preedit / no focus / sensitive) — "
                                    "不回退到普通 CommitText，避免覆盖用户预编辑"
                                ),
                            }
                        )
                    else:
                        asr_path = "streaming_stale_suppressed"
                        context.on_state(
                            {
                                "event": "log",
                                "message": (
                                    "stream_commit_suppressed: session stale "
                                    "(focus lost / user typed)"
                                ),
                            }
                        )
                else:
                    context.on_state(
                        {
                            "event": "log",
                            "message": (
                                "asr_stream_commit_fallback: empty_stream "
                                f"provider={asr_provider} caps={asr_capabilities}"
                            ),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                context.on_state(
                    {
                        "event": "log",
                        "message": (
                            f"asr_stream_commit_fallback: {type(exc).__name__}: {exc} "
                            f"provider={asr_provider} caps={asr_capabilities}"
                        ),
                    }
                )

        if not streamed_commit and _commit_suppresses_fallback(
            context.prefetched_commit_info,
            composition_started=context.prefetched_composition_started,
        ):
            # The realtime session ended terminally (focus lost / reset /
            # user typed / uncertain commit reply). Neither a fallback
            # transcription commit nor a second streaming pass is safe: the
            # toolkit may have committed the preedit itself, and the focus
            # may now be elsewhere. The text is still emitted below so the
            # UI/log can show it for manual copy — it is just never written
            # into a new focus automatically.
            commit_info = dict(context.prefetched_commit_info or {})
            streamed_commit = True
            asr_path = "realtime_stale_suppressed"
            prefetched_text = str(getattr(context, "prefetched_asr_text", "") or "")
            if prefetched_text.strip():
                # Keep the transcript visible for the UI/log (copyable),
                # never as an auto-commit candidate.
                raw_text = prefetched_text
                text = context.normalize_final_text(raw_text)
                detected_language = str(getattr(context, "prefetched_detected_language", "") or "").strip()
                transcribe_latency_ms = float(getattr(context, "prefetched_transcribe_latency_ms", 0.0) or 0.0)
            context.on_state(
                {
                    "event": "log",
                    "message": (
                        "realtime_commit_suppressed: "
                        f"{commit_info.get('detail', '')} — 不做回退提交，避免重复/错窗口；"
                        "原文保留在结果中供手动复制"
                    ),
                }
            )

        if (
            not streamed_commit
            and context.prefetched_outcome == "released_for_refine"
            and context.composition_session is not None
        ):
            # The realtime worker kept its composition session alive for the
            # refiner. Refine the (already SemIf-judged) text and commit
            # exactly once through the SAME session token: this preserves
            # the binding to the original input context even if the user
            # clicked elsewhere during refinement — a stale/replaced session
            # then fails the commit instead of writing into the new focus,
            # and no fallback path may retry afterwards.
            streamed_commit = True
            asr_path = "realtime_refine_session_commit"
            raw_text = str(getattr(context, "prefetched_asr_text", "") or "")
            text = context.normalize_final_text(raw_text)
            detected_language = str(getattr(context, "prefetched_detected_language", "") or "").strip()
            transcribe_latency_ms = float(getattr(context, "prefetched_transcribe_latency_ms", 0.0) or 0.0)
            pre_correction_text = text
            text = _apply_hotword_correction(
                text,
                args=context.args,
                hotwords=effective_hotwords,
                on_state=context.on_state,
            )
            hotword_corrected = text != pre_correction_text
            if context.refiner is not None and text.strip():
                text, refine_latency_ms = _run_refinement(
                    args=context.args,
                    refiner=context.refiner,
                    text=text,
                    effective_hotwords=effective_hotwords,
                    refine_postprocess_rule=context.refine_postprocess_rule,
                    on_state=context.on_state,
                )
            commit_info = _session_final_commit(
                context.composition_session,
                resolve_streaming_committer(context.committer),
                text.strip(),
                auto_hard_enter=auto_hard_enter,
            )
            if not bool(commit_info.get("committed")):
                context.on_state(
                    {
                        "event": "log",
                        "message": (
                            "refine_session_commit_failed: "
                            f"{commit_info.get('detail', '')} outcome={commit_info.get('outcome', '')}"
                            " — 会话终止，不回退到全局提交；文本保留供手动复制"
                        ),
                    }
                )

        if not streamed_commit:
            prefetched_text = str(getattr(context, "prefetched_asr_text", "") or "")
            if prefetched_text.strip():
                raw_text = prefetched_text
                text = context.normalize_final_text(raw_text)
                detected_language = str(getattr(context, "prefetched_detected_language", "") or "").strip()
                transcribe_latency_ms = float(getattr(context, "prefetched_transcribe_latency_ms", 0.0) or 0.0)
                asr_path = "prefetched"
            else:
                asr = context.provider.transcribe_file(context.audio_path, hotwords=effective_hotwords)
                transcribe_latency_ms = (time.perf_counter() - t0) * 1000
                raw_text = getattr(asr, "text", "")
                detected_language = str(getattr(asr, "detected_language", "") or "").strip()
                text = context.normalize_final_text(raw_text)

            # Deterministic hotword correction runs before refine so the
            # refiner sees canonical terms (and protects them), and before
            # commit when no refiner is configured. The bounded end-of-
            # sentence SemIf judgment runs here too, unless the realtime
            # worker already applied it to this exact text.
            pre_correction_text = text
            text = _apply_hotword_correction(
                text,
                args=context.args,
                hotwords=effective_hotwords,
                on_state=context.on_state,
            )
            if not context.prefetched_semif_applied:
                text = _apply_final_semif(
                    text,
                    args=context.args,
                    hotwords=effective_hotwords,
                    on_state=context.on_state,
                )
            hotword_corrected = text != pre_correction_text

            if (
                routing.commit_local
                and _streaming_commit_enabled(context.args)
                and context.refiner is not None
                and text.strip()
                and context.prefetched_commit_info is None
                and hasattr(context.refiner, "refine_stream")
                and not _should_skip_llm_refine(context.args, text)
            ):
                try:
                    text, refine_latency_ms, commit_info = _run_refinement_streaming_commit(
                        context=context,
                        text=text,
                        effective_hotwords=effective_hotwords,
                        auto_hard_enter=auto_hard_enter,
                    )
                    streamed_commit = True
                except Exception as exc:  # noqa: BLE001
                    context.on_state(
                        {
                            "event": "log",
                            "message": (
                                f"refine_stream_commit_fallback: {type(exc).__name__}: {exc} "
                                f"provider={asr_provider} caps={asr_capabilities}"
                            ),
                        }
                    )

            if not streamed_commit:
                if context.refiner and text.strip():
                    text, refine_latency_ms = _run_refinement(
                        args=context.args,
                        refiner=context.refiner,
                        text=text,
                        effective_hotwords=effective_hotwords,
                        refine_postprocess_rule=context.refine_postprocess_rule,
                        on_state=context.on_state,
                    )
                    if context.args.debug_diagnostics:
                        context.on_state(
                            {
                                "event": "log",
                                "message": (
                                    f"text_refine original_len={len(raw_text)}"
                                    f" refined_len={len(text)}"
                                    f" latency_ms={refine_latency_ms:.1f}"
                                    f" streaming={getattr(context.args, 'enable_streaming_refine', False)}"
                                ),
                            }
                        )
                elif bool(getattr(context.args, "enable_text_refine", False)) and text.strip():
                    context.on_state(
                        {
                            "event": "log",
                            "message": "text_refine_enabled_but_unavailable: refiner_not_initialized",
                        }
                    )

                prefetched_commit = context.prefetched_commit_info
                if (
                    routing.commit_local
                    and isinstance(prefetched_commit, dict)
                    and bool(prefetched_commit.get("committed"))
                ):
                    commit_info = dict(prefetched_commit)
                    streamed_commit = True
                elif routing.commit_local:
                    commit_info = _commit_text(
                        context.committer,
                        text,
                        auto_hard_enter=auto_hard_enter,
                    )
                else:
                    commit_info = {
                        "backend": "remote-paste",
                        "committed": False,
                        "detail": "routed_to_remote_paste",
                    }
        remote_result = send_remote_paste_from_args(
            context.args,
            text,
            log=lambda message: context.on_state({"event": "log", "message": message}),
        )
        if remote_result.get("enabled"):
            commit_info["remote_paste"] = remote_result
        if not routing.commit_local:
            commit_info.update(_remote_only_commit_info(remote_result))
        if auto_hard_enter and "hard_enter_failed" in str(commit_info.get("detail", "")):
            context.on_state({"event": "log", "message": f"auto_hard_enter_failed: {commit_info.get('detail', '')}"})
        if context.args.debug_diagnostics:
            context.on_state(
                {
                    "event": "log",
                    "message": (
                        f"diag finalize source={'streaming' if streamed_commit else 'oneshot'}"
                        f" asr_path={asr_path}"
                        f" asr_provider={asr_provider}"
                        f" asr_caps={asr_capabilities}"
                        f" detected_language={detected_language or 'unknown'}"
                        f" text_len={len(text)}"
                        f" committed={bool(commit_info.get('committed', False))}"
                        f" commit_backend={commit_info.get('backend', '')}"
                        f" commit_detail={commit_info.get('detail', '')}"
                        f" hotword_count={len(effective_hotwords)}"
                        f" text={_preview_text(text)}"
                    ),
                }
            )
        if context.auto_lexicon is not None and bool(commit_info.get("committed")) and text.strip():
            try:
                lexicon_source = _auto_lexicon_source(
                    refiner=context.refiner,
                    raw_text=raw_text,
                    final_text=text,
                    hotword_corrected=hotword_corrected,
                )
                learned_terms = _observe_auto_lexicon(
                    context.auto_lexicon,
                    text,
                    source=lexicon_source,
                )
                if context.args.debug_diagnostics:
                    context.on_state({"event": "log", "message": f"diag auto_lexicon_learned_terms={learned_terms} source={lexicon_source}"})
            except Exception as exc:  # noqa: BLE001
                if context.args.debug_diagnostics:
                    context.on_state({"event": "log", "message": f"diag auto_lexicon_learn_failed: {exc}"})

        _capture_refine_sample(
            args=context.args,
            audio_path=context.audio_path,
            record_backend=context.record_backend,
            raw_text=raw_text,
            final_text=text,
            refiner=context.refiner,
            transcribe_latency_ms=transcribe_latency_ms,
            refine_latency_ms=refine_latency_ms,
            commit_info=commit_info,
            on_state=context.on_state,
        )

        _emit_result(
            context.on_result,
            audio_path=context.audio_path,
            record_backend=context.record_backend,
            record_latency_ms=context.record_latency_ms,
            transcribe_latency_ms=transcribe_latency_ms,
            refine_latency_ms=refine_latency_ms,
            text=text,
            detected_language=detected_language,
            asr_provider=asr_provider,
            asr_path=asr_path,
            asr_capabilities=asr_capabilities,
            commit=commit_info,
        )
    except Exception as exc:  # noqa: BLE001
        context.on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
