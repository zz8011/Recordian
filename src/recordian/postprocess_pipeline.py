from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio import read_wav_mono_f32
from .linux_commit import paste_to_enter_delay_seconds, resolve_streaming_committer, send_hard_enter
from .remote_paste.client import resolve_remote_paste_routing, send_remote_paste_from_args

EventCallback = Callable[[dict[str, object]], None]


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
                "commit": commit,
            },
        }
    )


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
    _sync_refiner_preset(args, refiner, on_state)

    t1 = time.perf_counter()
    base_prompt_template = getattr(refiner, "prompt_template", None)
    protected_terms = _select_refine_protected_terms(text, effective_hotwords)
    prompt_with_guards = _build_refine_prompt_with_protected_terms(base_prompt_template, protected_terms)
    if prompt_with_guards != base_prompt_template:
        refiner.prompt_template = prompt_with_guards
    if args.debug_diagnostics and protected_terms:
        on_state({"event": "log", "message": f"diag refine_protected_terms={protected_terms}"})

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
        on_state({"event": "log", "message": f"text_refine_failed: {type(exc).__name__}: {exc}"})
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


def _run_asr_streaming_commit(
    *,
    context: "PostprocessPipelineContext",
    effective_hotwords: list[str],
    auto_hard_enter: bool,
) -> tuple[str, float, dict[str, object]]:
    if not hasattr(context.provider, "transcribe_file_stream"):
        raise NotImplementedError("asr_stream_unsupported")

    accumulator = _StreamingCommitAccumulator(resolve_streaming_committer(context.committer))
    raw_streamed_text = ""
    committed_text = ""
    t0 = time.perf_counter()
    for chunk in context.provider.transcribe_file_stream(context.audio_path, hotwords=effective_hotwords):
        raw_streamed_text = _merge_stream_text(raw_streamed_text, str(chunk))
        normalized_text = context.normalize_final_text(raw_streamed_text)
        committed_text, delta = _stream_display_delta(committed_text, normalized_text)
        if delta:
            accumulator.append_chunk(delta)
            context.on_state(
                {
                    "event": "stream_partial",
                    "chunk": delta,
                    "text": committed_text,
                }
            )
    transcribe_latency_ms = (time.perf_counter() - t0) * 1000
    final_text = committed_text.strip()
    commit_info = accumulator.finalize(final_text=final_text, auto_hard_enter=auto_hard_enter)
    return final_text, transcribe_latency_ms, commit_info


def _run_refinement_streaming_commit(
    *,
    context: "PostprocessPipelineContext",
    text: str,
    effective_hotwords: list[str],
    auto_hard_enter: bool,
) -> tuple[str, float, dict[str, object]]:
    refiner = context.refiner
    assert refiner is not None
    _sync_refiner_preset(context.args, refiner, context.on_state)

    base_prompt_template = getattr(refiner, "prompt_template", None)
    protected_terms = _select_refine_protected_terms(text, effective_hotwords)
    prompt_with_guards = _build_refine_prompt_with_protected_terms(base_prompt_template, protected_terms)
    if prompt_with_guards != base_prompt_template:
        refiner.prompt_template = prompt_with_guards
    if context.args.debug_diagnostics and protected_terms:
        context.on_state({"event": "log", "message": f"diag refine_protected_terms={protected_terms}"})

    context.on_state({"event": "log", "message": f"ASR 原始输出: {text}"})
    accumulator = _StreamingCommitAccumulator(resolve_streaming_committer(context.committer))
    refined_text = ""
    t1 = time.perf_counter()
    try:
        for chunk in refiner.refine_stream(text):
            token = str(chunk)
            if not token:
                continue
            refined_text += token
            accumulator.append_chunk(token)
            context.on_state(
                {
                    "event": "refine_stream_chunk",
                    "chunk": token,
                    "accumulated": refined_text,
                }
            )
    finally:
        if prompt_with_guards != base_prompt_template:
            refiner.prompt_template = base_prompt_template

    refine_latency_ms = (time.perf_counter() - t1) * 1000
    final_text = refined_text.strip()
    if final_text:
        context.on_state({"event": "log", "message": f"精炼后输出: {final_text}"})
    commit_info = accumulator.finalize(final_text=final_text, auto_hard_enter=auto_hard_enter)
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
    prefetched_transcribe_latency_ms: float = 0.0
    prefetched_commit_info: dict[str, object] | None = None


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
        transcribe_latency_ms = 0.0
        refine_latency_ms = 0.0
        commit_info: dict[str, object]

        if routing.commit_local:
            _apply_target_window(context.committer, context.state)

        streamed_commit = False
        if routing.commit_local and _streaming_commit_enabled(context.args) and context.refiner is None:
            try:
                text, transcribe_latency_ms, commit_info = _run_asr_streaming_commit(
                    context=context,
                    effective_hotwords=effective_hotwords,
                    auto_hard_enter=auto_hard_enter,
                )
                raw_text = text
                streamed_commit = True
            except Exception as exc:  # noqa: BLE001
                context.on_state({"event": "log", "message": f"asr_stream_commit_fallback: {type(exc).__name__}: {exc}"})

        if not streamed_commit:
            prefetched_text = str(getattr(context, "prefetched_asr_text", "") or "")
            if prefetched_text.strip():
                raw_text = prefetched_text
                text = context.normalize_final_text(raw_text)
                transcribe_latency_ms = float(getattr(context, "prefetched_transcribe_latency_ms", 0.0) or 0.0)
            else:
                asr = context.provider.transcribe_file(context.audio_path, hotwords=effective_hotwords)
                transcribe_latency_ms = (time.perf_counter() - t0) * 1000
                raw_text = getattr(asr, "text", "")
                text = context.normalize_final_text(raw_text)

            if (
                routing.commit_local
                and _streaming_commit_enabled(context.args)
                and context.refiner is not None
                and text.strip()
                and context.prefetched_commit_info is None
                and hasattr(context.refiner, "refine_stream")
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
                    context.on_state({"event": "log", "message": f"refine_stream_commit_fallback: {type(exc).__name__}: {exc}"})

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

                if routing.commit_local and context.prefetched_commit_info is not None:
                    commit_info = dict(context.prefetched_commit_info)
                    streamed_commit = bool(commit_info.get("committed", False))
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
                learned_terms = context.auto_lexicon.observe_accepted(text)
                if context.args.debug_diagnostics:
                    context.on_state({"event": "log", "message": f"diag auto_lexicon_learned_terms={learned_terms}"})
            except Exception as exc:  # noqa: BLE001
                if context.args.debug_diagnostics:
                    context.on_state({"event": "log", "message": f"diag auto_lexicon_learn_failed: {exc}"})

        _emit_result(
            context.on_result,
            audio_path=context.audio_path,
            record_backend=context.record_backend,
            record_latency_ms=context.record_latency_ms,
            transcribe_latency_ms=transcribe_latency_ms,
            refine_latency_ms=refine_latency_ms,
            text=text,
            commit=commit_info,
        )
    except Exception as exc:  # noqa: BLE001
        context.on_error({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
