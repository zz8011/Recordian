#!/usr/bin/env python3
"""通用 SystemOne choice 模型对照基准（Recordian 语境别名 role 判定）。

它做什么
--------
对 ``cases.json`` 里每一条合成句子，按 Recordian ``streaming_correction`` 的
真实请求形状**串行**发一次 HTTP ``choice`` 请求（payload 只有 ``state`` 与
``questions``；顶层不带 ``temperature``/``model``，产品也不带），并记录：

* 每条请求的完整合成 payload 与规范 sha256（无隐私文本，可逐字节复核）；
* 逐项对照产品模板参数的检查（F1：``heard`` 取匹配到的 surface，与
  ``streaming_correction._judge`` 一致）；
* 原始分布、``debug_choice`` 与坏分布原因（分布非法时原始准确率不计正确）；
* 产品 ``interpret_choice`` 0.70/0.20 门槛重放与结构保护重放（F4：产品模块
  不可用时 correctness=null，``guard_replay=unavailable``）；
* 延迟（成功 / 全部 / 失败分开统计，超时按真实耗时计入）与 ``--budget-ms``
  预算内计数。

命名与边界
----------
* 本工具是**通用 SystemOne 对照基准**：同一语料可对任何接受
  ``POST /v1/systemone`` choice 形状的模型运行，不绑定单一服务。
* 所有“改写”结论都是**结构保护与概率门槛重放**，不是线上真实改写：本工具
  没有运行完整流式 correction、没有 IME、没有产品异步 deadline。
* 顶层 ``temperature``/``model`` 不发送；各服务对这两个字段含义不同。运行
  元数据用 CLI 逐模型记录**经核实的部署配置与发布温度**（未核实写 ``unknown``
  并显著标注）；对照按各自发布配置重放行为，单个 run 不宣布两个 run 可比。
* 数字/端口/网址的本机规则不在评估范围内；语料全部为手写合成句。

用法示例
--------
python3 benchmark.py --cases cases.json --validate-only
python3 benchmark.py --endpoint http://HOST:PORT/v1/systemone --cases cases.json \\
    --output report.json --timeout 3.0 --retries 0 --rounds 2 --warm-new-round \\
    --budget-ms 350 --model-label <model-id> --effective-temperature unknown
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORT_SCHEMA = "systemone_decision_benchmark/2"
DEFAULT_TIMEOUT_S = 3.0  # 诊断用捕获超时；产品自身 deadline 远小于此
DEFAULT_RETRIES = 0
DEFAULT_RETRY_DELAY_S = 0.3
DEFAULT_ROUNDS = 1
DEFAULT_BUDGET_MS = 350.0
MAX_CONTEXT_CHARS = 256  # 产品 _CONTEXT_LIMIT
CLAUSE_BOUNDARY_RE = re.compile(r"[，。！？；：、,.!?;:\n]+")
EXPECTED_LABELS = ("tool", "person", "unclear")
GUARD_REASONS = (
    "ask",
    "skip_person",
    "skip_meta",
    "skip_negation",
    "skip_no_cue",
    "skip_repeat_in_clause",
    "skip_protected",
    "skip_other_clause_veto",
    "unknown",
)
GUARD_UNAVAILABLE = "unavailable"
# 重放动作：结构保护 + 概率门槛重放后，产品代码路径会做什么。
REPLAY_WOULD_REPLACE = "would_replace"
REPLAY_KEPT_BY_GUARD = "kept_no_request_by_guard"
REPLAY_KEPT_WEAK = "kept_weak_answer"
REPLAY_KEPT_BAD_DIST = "kept_bad_distribution"
REPLAY_KEPT_ERROR = "kept_error_no_verdict"
REPLAY_UNAVAILABLE = "guard_replay_unavailable"
# 只在无法 import 产品常量时使用的兜底快照；报告会记录用的是哪一份。
FALLBACK_CRITERIA = {
    "tool": "这里指的是软件工具、程序或插件。",
    "person": "这里指的是一个人。",
    "unclear": "不清楚，或者以上都不是。",
}
FALLBACK_INSTRUCTIONS = (
    "判断句子中标记的「{surface}」在这个语境里指的是什么。"
    "只能从给定候选里选一个。拿不准就选“不清楚”。"
    "补充说明：用户的常用词里，{word} 是这个用户的{meaning}；"
    "语音识别经常把它误写成 {heard}。"
)
FALLBACK_MIN_TOP = 0.70
FALLBACK_MIN_MARGIN = 0.20
PAYLOAD_SHAPE_PRODUCT = "product_judge_heard_surface"
PAYLOAD_SHAPE_LEGACY = "legacy_lexicon_heard"

try:  # 可选依赖：产品本身用 requests，缺了就退回标准库 urllib
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# 数值与小工具
# --------------------------------------------------------------------------
def percentile(values: list[float], q: float) -> float | None:
    """线性插值百分位，q ∈ [0, 100]；空列表返回 None。"""
    if not values:
        return None
    if q <= 0:
        return min(values)
    if q >= 100:
        return max(values)
    ordered = sorted(values)
    idx = (len(ordered) - 1) * (q / 100.0)
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    weight = idx - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """规范 JSON：键排序、无多余空白、保留非 ASCII 原文。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


# --------------------------------------------------------------------------
# 产品桥接：优先使用真实模块；失败时退回本地快照并如实记录
# --------------------------------------------------------------------------
class ProductBridge:
    """封装 product 侧的 request/门槛/结构保护逻辑。"""

    def __init__(self, src_path: str | None) -> None:
        self.source = "fallback_vendored"
        self.import_error: str | None = None
        self.module_hashes: dict[str, str | None] = {}
        self.criteria: dict[str, str] = dict(FALLBACK_CRITERIA)
        self.instructions_template = FALLBACK_INSTRUCTIONS
        self.min_top = FALLBACK_MIN_TOP
        self.min_margin = FALLBACK_MIN_MARGIN
        self.gate_source = "fallback_vendored"
        self._interpret_choice = None
        self._alias_spans = None
        self._protected_spans = None
        self._meta_mention = None
        self._negated_before = None
        self._bare_person_alias = None
        self._clause_spans = None
        self._clause_index = None
        self._software_context_eligible = None
        self._overlaps_span = None
        self._jev_command = None

        candidates: list[Path] = []
        if src_path:
            candidates.append(Path(src_path))
        env_src = os.environ.get("RECORDIAN_SRC")
        if env_src:
            candidates.append(Path(env_src))
        candidates.append(Path(__file__).resolve().parents[2] / "src")
        self.src_path = None
        for candidate in candidates:
            if (candidate / "recordian" / "semif_judge.py").is_file():
                self.src_path = str(candidate)
                break
        if self.src_path and self.src_path not in sys.path:
            sys.path.insert(0, self.src_path)
        try:
            self._load_product_modules()
        except Exception as exc:  # noqa: BLE001 - 任何 import 失败都退回快照
            self.import_error = f"{type(exc).__name__}: {exc}"

    @property
    def product_path_available(self) -> bool:
        """只有真的导入了产品模块，结构保护重放才可用（F4）。"""
        return self.source == "imported:recordian" and self._alias_spans is not None

    def _load_product_modules(self) -> None:
        from recordian.hotword_corrector import _protected_spans
        from recordian.semif_judge import interpret_choice
        from recordian.streaming_correction import (
            _ROLE_CRITERIA,
            _ROLE_INSTRUCTIONS,
            _alias_spans,
            _bare_person_alias,
            _clause_index,
            _clause_spans,
            _meta_mention,
            _negated_before,
            _overlaps_span,
            software_context_eligible,
        )

        try:  # 拒绝门槛以产品常量为准，读不到才用兜底快照
            from recordian.semif_judge import _MIN_MARGIN, _MIN_TOP
        except ImportError:
            _MIN_TOP, _MIN_MARGIN = FALLBACK_MIN_TOP, FALLBACK_MIN_MARGIN
            self.gate_source = "fallback_vendored"
        else:
            self.gate_source = "imported:recordian.semif_judge"

        try:
            from recordian.jev_judge import _command as jev_command
        except ImportError:  # 可选路径
            jev_command = None

        self._interpret_choice = interpret_choice
        self.criteria = dict(_ROLE_CRITERIA)
        self.instructions_template = str(_ROLE_INSTRUCTIONS)
        self.min_top = float(_MIN_TOP)
        self.min_margin = float(_MIN_MARGIN)
        self._protected_spans = _protected_spans
        self._meta_mention = _meta_mention
        self._negated_before = _negated_before
        self._bare_person_alias = _bare_person_alias
        self._alias_spans = _alias_spans
        self._clause_spans = _clause_spans
        self._clause_index = _clause_index
        self._software_context_eligible = software_context_eligible
        self._overlaps_span = _overlaps_span
        self._jev_command = jev_command
        self.source = "imported:recordian"
        root = Path(self.src_path or ".")
        for name in ("semif_judge.py", "streaming_correction.py", "hotword_corrector.py", "benchmark.py"):
            path = root / "recordian" / name
            if path.is_file():
                self.module_hashes[f"recordian/{name}"] = sha256_file(path)

    # -- 门槛 ---------------------------------------------------------------
    def interpret_choice(self, options: dict[str, str], picked: object) -> str | None:
        if self._interpret_choice is not None:
            return self._interpret_choice(options, picked)  # type: ignore[no-any-return]
        return _vendored_interpret_choice(options, picked)

    # -- 结构保护 -----------------------------------------------------------
    def guard_reason(self, text: str, start: int, end: int, context: str, alias: dict[str, str]) -> str:
        """产品是否会对这次出现发 role 请求；返回 ask、跳过原因或 unavailable。"""
        if not self.product_path_available or self._alias_spans is None:
            return GUARD_UNAVAILABLE
        heard = alias["heard"]
        word = alias["word"]
        meaning = alias["meaning"]
        spans = self._alias_spans(text, [(heard, word, meaning)], context)
        if any(int(s["start"]) == start and int(s["end"]) == end for s in spans):
            return "ask"
        protected = self._protected_spans(text)  # type: ignore[misc]
        if self._overlaps_span(start, end, protected):  # type: ignore[misc]
            return "skip_protected"
        if self._meta_mention(text, start, end):  # type: ignore[misc]
            return "skip_meta"
        if self._negated_before(text, start):  # type: ignore[misc]
            return "skip_negation"
        if self._bare_person_alias(text, start, end):  # type: ignore[misc]
            return "skip_person"
        clauses = self._clause_spans(text)  # type: ignore[misc]
        index = self._clause_index(clauses, start)  # type: ignore[misc]
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(heard)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        admissible = []
        for match in pattern.finditer(text):
            m_start, m_end = match.start(), match.end()
            if (
                self._overlaps_span(m_start, m_end, protected)  # type: ignore[misc]
                or self._meta_mention(text, m_start, m_end)  # type: ignore[misc]
                or self._negated_before(text, m_start)  # type: ignore[misc]
                or self._bare_person_alias(text, m_start, m_end)  # type: ignore[misc]
            ):
                continue
            admissible.append(match)
        same_clause = [m for m in admissible if self._clause_index(clauses, m.start()) == index]  # type: ignore[misc]
        if len(same_clause) != 1:
            return "skip_repeat_in_clause"
        begin, stop = clauses[index]
        if not self._software_context_eligible(text[begin:stop], context):  # type: ignore[misc]
            return "skip_no_cue"
        return "skip_other_clause_veto"

    def jev_argv(self, timeout_s: float, argv: list[str] | None) -> list[str] | None:
        """复用产品 jev_judge._command 的 argv 约定（CLI 自己读 key，本工具不碰凭据）。"""
        if self._jev_command is not None:
            return self._jev_command(argv, timeout_s)  # type: ignore[no-any-return]
        if argv:
            base = [str(part) for part in argv if str(part)]
            return [*base, "--timeout", f"{max(0.05, timeout_s):.3f}"] if base else None
        return None


def _vendored_interpret_choice(options: dict[str, str], picked: object) -> str | None:
    """与 recordian.semif_judge.interpret_choice 同逻辑的兜底副本。"""
    if not isinstance(picked, dict) or not options:
        return None
    choice = picked.get("choice")
    if not isinstance(choice, str) or choice not in options:
        return None
    probabilities = picked.get("probabilities")
    if not isinstance(probabilities, dict):
        return None
    if set(probabilities) != set(options):
        return None
    scores: dict[str, float] = {}
    for name, raw in probabilities.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        score = float(raw)
        if not math.isfinite(score) or score < 0.0:
            return None
        scores[str(name)] = score
    total = sum(scores.values())
    if abs(total - 1.0) > 1e-3:
        return None
    ranking = sorted(scores.items(), key=lambda item: item[1])
    top_name, top_score = ranking[-1]
    second = ranking[-2][1] if len(ranking) > 1 else 0.0
    if top_name != choice or top_score < FALLBACK_MIN_TOP or top_score - second < FALLBACK_MIN_MARGIN:
        return None
    return choice


# --------------------------------------------------------------------------
# 请求构造：与产品 streaming_correction._judge 逐项对齐
# --------------------------------------------------------------------------
def clause_of(text: str, position: int) -> str:
    start = 0
    for match in CLAUSE_BOUNDARY_RE.finditer(text):
        if start <= position < match.start():
            return text[start : match.start()]
        start = match.end()
    return text[start:]


def build_state(context: str, focus: str, surface: str) -> str:
    """产品 StreamingHotwordCorrector._state(job, surface, focus) 的请求形状。"""
    lines = []
    if context:
        lines.append(f"上一段：{context}")
    lines.append(focus)
    lines.append(f"只判断这个跨度：「{surface}」")
    return "\n".join(lines)


def product_state_replica(context: str, focus: str, surface: str) -> str:
    """_state 的独立字面复刻，用来交叉核对 build_state 没有漂移。"""
    lines: list[str] = []
    if context:
        lines.append("上一段：" + context)
    lines.append(focus)
    lines.append("只判断这个跨度：「" + surface + "」")
    return "\n".join(lines)


def same_clause_alias_occurrences(text: str, start: int, heard: str) -> int:
    """目标小句内同一别名的出现次数（结构歧义检测，不依赖产品模块）。"""
    clause_start = 0
    cursor = 0
    for match in CLAUSE_BOUNDARY_RE.finditer(text):
        if cursor <= start < match.start():
            clause_start = cursor
            break
        cursor = match.end()
    else:
        clause_start = cursor
    clause_end = len(text)
    for match in CLAUSE_BOUNDARY_RE.finditer(text, clause_start):
        clause_end = match.start()
        break
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(heard)}(?![A-Za-z0-9_])", re.IGNORECASE)
    return sum(1 for _ in pattern.finditer(text[clause_start:clause_end]))


def build_request(
    case_like: dict[str, Any],
    *,
    bridge: ProductBridge,
    alias: dict[str, str],
    compat_lexicon_heard: bool = False,
) -> dict[str, Any]:
    """构造一次 role 请求，并逐项核对产品模板参数。

    F1：产品的 ``_judge`` 把 ``heard`` 填成匹配到的 surface，本函数默认照做；
    ``compat_lexicon_heard=True`` 只用于复现修正前的旧证据，报告会标记为非产品形状。
    """
    text = str(case_like["text"])
    focus = str(case_like["focus"])
    context = str(case_like.get("context", "") or "")
    span = case_like["target_span"]
    surface = str(span["surface"])
    start, end = int(span["start"]), int(span["end"])
    word = str(alias["word"])
    meaning = str(alias["meaning"])
    heard = str(alias["heard"]) if compat_lexicon_heard else surface
    params = {"surface": surface, "word": word, "meaning": meaning, "heard": heard}
    instructions = bridge.instructions_template.format(**params)
    criteria = dict(bridge.criteria)
    questions = {"role": {"type": "choice", "instructions": instructions, "criteria": criteria}}
    state = build_state(context, focus, surface)
    payload = {"state": state, "questions": questions}
    checks = {
        "surface_matches_text_span": surface == text[start:end],
        "heard_is_surface_like_product_judge": heard == surface,
        "word_matches_alias": word == str(alias.get("word")),
        "meaning_matches_alias": meaning == str(alias.get("meaning")),
        "instructions_matches_product_template": instructions == bridge.instructions_template.format(**params),
        "criteria_match_product_role_criteria": criteria == dict(bridge.criteria),
        "question_type_is_choice": questions["role"]["type"] == "choice",
        "question_ids_are_role_only": set(questions) == {"role"},
        "state_matches_product_state_template": state == product_state_replica(context, focus, surface),
        "payload_keys_are_state_and_questions": set(payload) == {"state", "questions"},
    }
    # legacy 模式只允许这一处已声明的偏离，其余检查仍然是硬门槛。
    deviations = ["heard_is_surface_like_product_judge"] if compat_lexicon_heard else []
    mismatches = [name for name, ok in checks.items() if not ok]
    return {
        "payload": payload,
        "payload_sha256": canonical_sha256(payload),
        "payload_shape": PAYLOAD_SHAPE_LEGACY if compat_lexicon_heard else PAYLOAD_SHAPE_PRODUCT,
        "state": state,
        "questions": questions,
        "instructions": instructions,
        "instructions_sha256": sha256_text(instructions),
        "product_params": params,
        "param_checks": checks,
        "param_mismatches": mismatches,
        "param_deviations": deviations,
        "param_mismatches_blocking": [name for name in mismatches if name not in deviations],
        "surface": surface,
        "focus": focus,
        "context": context,
    }


# --------------------------------------------------------------------------
# 语料校验
# --------------------------------------------------------------------------
def validate_corpus(
    data: object,
    path: Path,
    *,
    bridge: ProductBridge,
    compat_lexicon_heard: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """返回 (problems, summary)。problems 非空时不应发起网络请求。"""
    problems: list[str] = []
    summary: dict[str, Any] = {"path": str(path), "sha256": sha256_file(path)}
    if not isinstance(data, dict):
        return ["cases.json 顶层不是对象"], summary
    alias = data.get("alias")
    if not isinstance(alias, dict) or not all(isinstance(alias.get(k), str) for k in ("heard", "word", "meaning")):
        problems.append("缺少 alias/{heard,word,meaning}")
        alias = {"heard": "jeff", "word": "jev", "meaning": "软件工具"}
    heard = str(alias["heard"])
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return problems + ["cases 为空"], summary
    occ_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(heard)}(?![A-Za-z0-9_])", re.IGNORECASE)
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    seen_alts: set[str] = set()
    main_payload_ids: dict[str, str] = {}
    duplicate_main_payloads: list[str] = []
    alt_payload_ids: dict[str, str] = {}
    alt_payload_collisions: list[str] = []
    param_mismatch_ids: list[str] = []
    deviation_ids: list[str] = []
    surface_differs: list[str] = []
    ambiguous_ids: list[str] = []
    flagged_exclusions: list[str] = []
    categories: dict[str, int] = {}
    expected_counts: dict[str, int] = {}
    guard_counts: dict[str, int] = {}
    negatives = 0
    with_alt = 0
    long_context = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            problems.append(f"case#{index} 不是对象")
            continue
        cid = str(case.get("id", f"case#{index}"))
        if cid in seen_ids:
            problems.append(f"{cid}: id 重复")
        seen_ids.add(cid)
        for field in ("text", "focus", "expected", "guard_expected"):
            if not isinstance(case.get(field), str) or not case.get(field):
                problems.append(f"{cid}: 缺少 {field}")
        text = str(case.get("text", ""))
        focus = str(case.get("focus", ""))
        context = str(case.get("context", "") or "")
        expected = str(case.get("expected", ""))
        guard_expected = str(case.get("guard_expected", ""))
        span = case.get("target_span")
        if not isinstance(span, dict):
            problems.append(f"{cid}: 缺少 target_span")
            continue
        try:
            start, end = int(span["start"]), int(span["end"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{cid}: target_span 不是整数区间")
            continue
        if not (0 <= start < end <= len(text)):
            problems.append(f"{cid}: target_span 越界 {start}:{end}")
            continue
        surface = str(span.get("surface", ""))
        if text[start:end] != surface:
            problems.append(f"{cid}: text[{start}:{end}]={text[start:end]!r} != surface {surface!r}")
        if not occ_re.fullmatch(surface):
            problems.append(f"{cid}: surface {surface!r} 不匹配别名 {heard!r}")
        elif surface != heard:
            surface_differs.append(cid)
        if not any(m.start() == start and m.end() == end for m in occ_re.finditer(text)):
            problems.append(f"{cid}: target_span 未落在别名出现上")
        if clause_of(text, start) != focus:
            problems.append(f"{cid}: focus 与 text 的小句不一致（产品会发不同请求）")
        if expected not in EXPECTED_LABELS:
            problems.append(f"{cid}: expected={expected!r} 非法")
        if guard_expected not in GUARD_REASONS:
            problems.append(f"{cid}: guard_expected={guard_expected!r} 非法")
        if len(context) > MAX_CONTEXT_CHARS:
            long_context += 1
            problems.append(f"{cid}: context 超过产品上限 {MAX_CONTEXT_CHARS}")
        if text in seen_texts:
            problems.append(f"{cid}: 句子与前面重复（会污染 cold 轮）")
        seen_texts.add(text)
        # --- F1/F3: 主轮完整 payload 唯一 ---------------------------------
        built = build_request(case, bridge=bridge, alias=alias, compat_lexicon_heard=compat_lexicon_heard)
        if built["param_mismatches_blocking"]:
            param_mismatch_ids.append(cid)
            problems.append(f"{cid}: 请求参数未通过产品模板核对 {built['param_mismatches_blocking']}")
        elif built["param_deviations"]:
            deviation_ids.append(cid)
        previous = main_payload_ids.get(built["payload_sha256"])
        if previous is not None:
            duplicate_main_payloads.append(f"{cid}=={previous}")
            problems.append(f"{cid}: 主轮 payload 与 {previous} 完全相同（独立样本被污染）")
        else:
            main_payload_ids[built["payload_sha256"]] = cid
        ambiguous = same_clause_alias_occurrences(text, start, heard) > 1 and guard_expected != "ask"
        if ambiguous:
            ambiguous_ids.append(cid)
            if not case.get("exclude_from_raw_accuracy"):
                problems.append(f"{cid}: 同小句多目标结构跳过样例未标记 exclude_from_raw_accuracy")
        if case.get("exclude_from_raw_accuracy"):
            flagged_exclusions.append(cid)
            if not ambiguous:
                problems.append(f"{cid}: 标了 exclude_from_raw_accuracy 但结构上不是同小句多目标跳过样例")
        alt = case.get("alt")
        if isinstance(alt, dict):
            alt_text = str(alt.get("text", ""))
            alt_span = alt.get("target_span")
            with_alt += 1
            if not alt_text or alt_text in seen_texts or alt_text in seen_alts:
                problems.append(f"{cid}: alt 句子空或重复")
            seen_alts.add(alt_text)
            if not isinstance(alt_span, dict):
                problems.append(f"{cid}: alt 缺少 target_span")
            else:
                try:
                    a_start, a_end = int(alt_span["start"]), int(alt_span["end"])
                except (KeyError, TypeError, ValueError):
                    problems.append(f"{cid}: alt target_span 非法")
                else:
                    if alt_text[a_start:a_end] != str(alt_span.get("surface", "")):
                        problems.append(f"{cid}: alt span 与 alt text 不一致")
                    if not any(m.start() == a_start and m.end() == a_end for m in occ_re.finditer(alt_text)):
                        problems.append(f"{cid}: alt target_span 未落在别名出现上")
                    if clause_of(alt_text, a_start) != str(alt.get("focus", "")):
                        problems.append(f"{cid}: alt focus 与 alt text 的小句不一致")
                    alt_case = {
                        "id": f"{cid}#alt",
                        "text": alt_text,
                        "focus": str(alt.get("focus", "")),
                        "context": context,
                        "target_span": {
                            "start": a_start,
                            "end": a_end,
                            "surface": str(alt_span.get("surface", "")),
                        },
                    }
                    alt_built = build_request(
                        alt_case, bridge=bridge, alias=alias, compat_lexicon_heard=compat_lexicon_heard
                    )
                    alt_id = f"{cid}#alt"
                    if alt_built["param_mismatches_blocking"]:
                        param_mismatch_ids.append(alt_id)
                        problems.append(
                            f"{alt_id}: 请求参数未通过产品模板核对 {alt_built['param_mismatches_blocking']}"
                        )
                    elif alt_built["param_deviations"]:
                        deviation_ids.append(alt_id)
                    if alt_built["payload_sha256"] in main_payload_ids:
                        ref = main_payload_ids[alt_built["payload_sha256"]]
                        alt_payload_collisions.append(f"{alt_id}==main:{ref}")
                        problems.append(f"{cid}: alt payload 与主轮 payload（{ref}）相同（warm 轮混入重复请求）")
                    if alt_built["payload_sha256"] in alt_payload_ids:
                        ref = alt_payload_ids[alt_built["payload_sha256"]]
                        alt_payload_collisions.append(f"{alt_id}=={ref}")
                        problems.append(f"{cid}: alt payload 与另一条 alt（{ref}）相同")
                    else:
                        alt_payload_ids[alt_built["payload_sha256"]] = alt_id
        category = str(case.get("category", "?"))
        categories[category] = categories.get(category, 0) + 1
        expected_counts[expected] = expected_counts.get(expected, 0) + 1
        guard_counts[guard_expected] = guard_counts.get(guard_expected, 0) + 1
        if expected != "tool":
            negatives += 1
    declared = data.get("question") if isinstance(data.get("question"), dict) else {}
    if declared:
        if str(declared.get("instructions_template", "")) != bridge.instructions_template:
            problems.append("cases.json question.instructions_template 与运行时产品模板不一致")
        if dict(declared.get("criteria", {}) or {}) != dict(bridge.criteria):
            problems.append("cases.json question.criteria 与运行时产品 criteria 不一致")
    if negatives < 30:
        problems.append(f"保留原文的负例只有 {negatives} 条，少于 30")
    if len(cases) < 60:
        problems.append(f"语料只有 {len(cases)} 条，少于 60")
    summary.update(
        {
            "case_count": len(cases),
            "negative_count": negatives,
            "alt_count": with_alt,
            "categories": dict(sorted(categories.items())),
            "expected_counts": dict(sorted(expected_counts.items())),
            "guard_expected_counts": dict(sorted(guard_counts.items())),
            "long_context_cases": long_context,
            "alias": {"heard": heard, "word": str(alias.get("word")), "meaning": str(alias.get("meaning"))},
            "payload_shape": PAYLOAD_SHAPE_LEGACY if compat_lexicon_heard else PAYLOAD_SHAPE_PRODUCT,
            "main_unique_payload_sha256_n": len(main_payload_ids),
            "main_duplicate_payload_refs": duplicate_main_payloads,
            "alt_unique_payload_sha256_n": len(alt_payload_ids),
            "alt_payload_collision_refs": alt_payload_collisions,
            "product_param_mismatch_ids": sorted(set(param_mismatch_ids)),
            "declared_param_deviation_ids": sorted(set(deviation_ids)),
            "surface_equals_lexicon_heard_n": len(cases) - len(surface_differs),
            "surface_differs_from_lexicon_heard_ids": surface_differs,
            "same_clause_multi_target_ids": ambiguous_ids,
            "raw_accuracy_excluded_ids": flagged_exclusions,
        }
    )
    return problems, summary


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------
class HttpTransport:
    """一次 POST 一个 role 问题；错误分类只用于统计，不当作弃权。"""

    def __init__(self, endpoint: str, timeout_s: float, user_agent: str) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self.session: Any = None
        if requests is not None:
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": user_agent})

    def send(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        payload = {"state": state, "questions": questions}
        started = time.perf_counter()
        if self.session is not None:
            try:
                response = self.session.post(self.endpoint, json=payload, timeout=self.timeout_s)
            except requests.Timeout:  # type: ignore[union-attr]
                return _attempt("timeout", started, error="requests.Timeout")
            except requests.ConnectionError as exc:  # type: ignore[union-attr]
                return _attempt("connect_error", started, error=type(exc).__name__)
            except requests.RequestException as exc:  # type: ignore[union-attr]
                return _attempt("transport_error", started, error=type(exc).__name__)
            status = int(response.status_code)
            if not 200 <= status < 300:
                return _attempt("http_error", started, http_status=status, error=f"HTTP {status}")
            try:
                body = response.json()
            except ValueError:
                return _attempt("malformed", started, http_status=status, error="响应不是 JSON")
            return _attempt("ok", started, http_status=status, body=body)
        # 标准库兜底
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                raw = response.read().decode("utf-8", "replace")
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return _attempt("http_error", started, http_status=int(exc.code), error=f"HTTP {exc.code}")
        except TimeoutError:
            return _attempt("timeout", started, error="socket timeout")
        except urllib.error.URLError as exc:
            return _attempt("connect_error", started, error=type(exc.reason).__name__)
        except OSError as exc:
            return _attempt("transport_error", started, error=type(exc).__name__)
        try:
            body = json.loads(raw)
        except ValueError:
            return _attempt("malformed", started, http_status=status, error="响应不是 JSON")
        return _attempt("ok", started, http_status=status, body=body)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


class JevTransport:
    """官方 Jev CLI 路径：只有显式 --provider jev 才会走到这里。

    本工具在 benchmark 侧重放同样的 stdin/stdout 协议，以便保留原始分布；
    argv 复用产品 ``jev_judge._command`` 的约定。密钥由 CLI 自己读取，
    本工具不读环境凭据、不把句子放进命令行。
    """

    _MAX_STDOUT = 1_000_000

    def __init__(self, bridge: ProductBridge, timeout_s: float, argv: list[str] | None) -> None:
        self.bridge = bridge
        self.timeout_s = timeout_s
        self.argv = argv

    def send(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        started = time.perf_counter()
        command = self.bridge.jev_argv(self.timeout_s, self.argv)
        if not command:
            return _attempt("transport_error", started, error="jev 命令不可用（缺少 argv 或 CLI）")
        payload = json.dumps(
            {"state": state, "questions": questions}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        env = os.environ.copy()
        env["SEMIF"] = "0"
        env["TYPESAFE_MODEL"] = "jev-latest"
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv 是列表，shell 关闭
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                shell=False,
            )
        except OSError as exc:
            return _attempt("transport_error", started, error=type(exc).__name__)
        try:
            stdout, _stderr = proc.communicate(payload, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return _attempt("timeout", started, error="jev timeout")
        except OSError as exc:
            proc.kill()
            return _attempt("transport_error", started, error=type(exc).__name__)
        if proc.returncode != 0:
            return _attempt("jev_error", started, error=f"jev exit {proc.returncode}")
        if not stdout or len(stdout) > self._MAX_STDOUT:
            return _attempt("malformed", started, error="jev 输出为空或过大")
        try:
            body = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _attempt("malformed", started, error="jev 输出不是 JSON")
        if not isinstance(body, dict) or "error" in body:
            return _attempt("malformed", started, error="jev 输出缺少 answers")
        return _attempt("ok", started, body=body)

    def close(self) -> None:
        return None


def _attempt(
    status: str,
    started: float,
    *,
    http_status: int | None = None,
    body: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "http_status": http_status,
        "body": body,
        "error": error,
        "duration_ms": (time.perf_counter() - started) * 1000.0,
    }


# --------------------------------------------------------------------------
# 单个请求与判定
# --------------------------------------------------------------------------
def classify_body(body: Any, criteria: dict[str, str]) -> dict[str, Any]:
    """解析 answers.role。

    F2：``probabilities`` 缺失或非法时，``answer_choice`` 清空（原始准确率不计
    正确），只保留 ``debug_choice`` 供诊断；坏分布单独计数。
    """
    result: dict[str, Any] = {
        "answer_choice": None,
        "debug_choice": None,
        "probabilities": None,
        "dist_valid": False,
        "invalid_reason": None,
        "confidence": None,
        "model": None,
        "engine_ms": None,
        "raw_distribution": True,
        "counted_in_raw_accuracy": False,
    }
    if not isinstance(body, dict):
        result["invalid_reason"] = "body_not_object"
        return result
    result["model"] = body.get("model") if isinstance(body.get("model"), str) else None
    usage = body.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("engine_ms"), (int, float)):
        result["engine_ms"] = float(usage["engine_ms"])
    if body.get("raw_distribution") is False:
        result["raw_distribution"] = False
    answers = body.get("answers")
    if not isinstance(answers, dict):
        result["invalid_reason"] = "missing_answers"
        return result
    picked = answers.get("role")
    if not isinstance(picked, dict):
        result["invalid_reason"] = "missing_role_answer"
        return result
    choice = picked.get("choice")
    if isinstance(choice, str):
        result["debug_choice"] = choice
    if isinstance(picked.get("confidence"), (int, float)):
        result["confidence"] = float(picked["confidence"])
    probabilities = picked.get("probabilities")
    if not isinstance(probabilities, dict):
        result["invalid_reason"] = "missing_probabilities"
        return result
    if set(probabilities) != set(criteria):
        result["invalid_reason"] = "probability_keys_mismatch"
        return result
    scores: dict[str, float] = {}
    for name, raw in probabilities.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            result["invalid_reason"] = "probability_not_number"
            return result
        score = float(raw)
        if not math.isfinite(score) or score < 0.0:
            result["invalid_reason"] = "probability_not_finite_or_negative"
            return result
        scores[str(name)] = score
    result["probabilities"] = scores
    if abs(sum(scores.values()) - 1.0) > 1e-3:
        result["invalid_reason"] = "probabilities_do_not_sum_to_one"
        return result
    if result["debug_choice"] not in criteria:
        result["invalid_reason"] = "choice_not_in_criteria"
        return result
    result["dist_valid"] = True
    result["answer_choice"] = result["debug_choice"]
    result["counted_in_raw_accuracy"] = True
    return result


def gate_decision(gate_result: str | None, dist_valid: bool, status: str) -> str:
    if status != "ok":
        return "no_answer_error"
    if not dist_valid:
        return "unusable_bad_distribution"
    if gate_result is None:
        return "reject_weak"
    return "accept"


def run_case(
    transport: HttpTransport | JevTransport,
    bridge: ProductBridge,
    alias: dict[str, str],
    case: dict[str, Any],
    *,
    source: str,
    retries: int,
    retry_delay_s: float,
    guard_replay: str,
    compat_lexicon_heard: bool = False,
) -> dict[str, Any]:
    built = build_request(case, bridge=bridge, alias=alias, compat_lexicon_heard=compat_lexicon_heard)
    state = str(built["state"])
    questions = built["questions"]
    surface = str(built["surface"])
    text = str(case["text"])
    expected = str(case["expected"])
    span = case["target_span"]
    ambiguous = same_clause_alias_occurrences(text, int(span["start"]), str(alias["heard"])) > 1
    record: dict[str, Any] = {
        "case_id": str(case["id"]),
        "category": str(case.get("category", "?")),
        "source": source,
        "expected": expected,
        "guard_expected": str(case.get("guard_expected", "")) if source == "main" else None,
        "guard_replay": guard_replay,
        "product_would_request": guard_replay == "ask",
        "guard_replay_available": guard_replay != GUARD_UNAVAILABLE,
        "target": {
            "start": int(span["start"]),
            "end": int(span["end"]),
            "surface": surface,
        },
        "context": str(case.get("context", "") or ""),
        "focus": str(case.get("focus", "")),
        "request_state": state,
        "request_payload": built["payload"],
        "request_payload_sha256": built["payload_sha256"],
        "instructions_sha256": built["instructions_sha256"],
        "product_params": built["product_params"],
        "payload_param_checks": built["param_checks"],
        "payload_param_mismatches": built["param_mismatches_blocking"],
        "payload_param_deviations": built["param_deviations"],
        "payload_shape": built["payload_shape"],
        "same_clause_multi_target": ambiguous,
        "raw_accuracy_eligible": not (ambiguous and guard_replay != "ask"),
        "attempts": [],
        "final_status": "no_attempt",
        "final_http_status": None,
        "duration_ms": None,
        "attempts_used": 0,
        "first_attempt_ok": None,
        "recovered_by_retry": False,
    }
    attempts_allowed = 1 + max(0, retries)
    final: dict[str, Any] = {}
    for attempt_index in range(1, attempts_allowed + 1):
        outcome = transport.send(state, questions)
        attempt = {
            "attempt": attempt_index,
            "status": outcome["status"],
            "http_status": outcome["http_status"],
            "duration_ms": round(float(outcome["duration_ms"]), 3),
            "error": outcome["error"],
        }
        record["attempts"].append(attempt)
        final = outcome
        if outcome["status"] == "ok":
            break
        if attempt_index < attempts_allowed and retry_delay_s > 0:
            time.sleep(retry_delay_s)
    attempts = record["attempts"]
    record["attempts_used"] = len(attempts)
    record["first_attempt_ok"] = bool(attempts and attempts[0]["status"] == "ok")
    record["final_status"] = str(final.get("status", "no_attempt"))
    record["final_http_status"] = final.get("http_status")
    record["duration_ms"] = round(float(final.get("duration_ms", 0.0)), 3)
    record["recovered_by_retry"] = bool(record["final_status"] == "ok" and len(attempts) > 1)
    parsed = classify_body(final.get("body"), bridge.criteria)
    record.update(parsed)
    if not parsed["counted_in_raw_accuracy"]:
        record["raw_correct"] = None
    else:
        record["raw_correct"] = bool(parsed["answer_choice"] == expected)
    gate = None
    if record["final_status"] == "ok" and parsed["dist_valid"]:
        gate = bridge.interpret_choice(
            bridge.criteria,
            {"choice": parsed["answer_choice"], "probabilities": parsed["probabilities"]},
        )
    record["gate_result"] = gate
    record["gate_decision"] = gate_decision(gate, bool(parsed["dist_valid"]), str(record["final_status"]))
    record["replay_action"], record["replay_correct"] = replay_action(record, expected, guard_replay, parsed, gate)
    return record


def replay_action(
    record: dict[str, Any],
    expected: str,
    guard_replay: str,
    parsed: dict[str, Any],
    gate: str | None,
) -> tuple[str, bool | None]:
    """结构保护重放 → 概率门槛重放 之后，产品代码路径会做什么。

    F4：产品模块不可用（``guard_replay=unavailable``）时不给 correctness，
    避免把“没跑成”当成“零误改”。
    """
    if guard_replay == GUARD_UNAVAILABLE:
        return REPLAY_UNAVAILABLE, None
    if guard_replay != "ask":
        return REPLAY_KEPT_BY_GUARD, expected != "tool"
    if record["final_status"] != "ok":
        return REPLAY_KEPT_ERROR, None
    if not parsed["dist_valid"]:
        return REPLAY_KEPT_BAD_DIST, None
    if gate == "tool":
        return REPLAY_WOULD_REPLACE, expected == "tool"
    if gate is None:
        return REPLAY_KEPT_WEAK, expected != "tool"
    return f"kept_verdict_{gate}", expected != "tool"


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------
def _latency_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    """延迟统计：成功 / 全部 / 失败分开，超时不会被排除。"""
    ok_ms = [float(r["duration_ms"]) for r in records if r["final_status"] == "ok"]
    failed_ms = [float(r["duration_ms"]) for r in records if r["final_status"] != "ok"]
    all_ms = [float(r["duration_ms"]) for r in records if isinstance(r.get("duration_ms"), (int, float))]
    all_attempt_ms: list[float] = []
    for r in records:
        for a in r["attempts"]:
            all_attempt_ms.append(float(a["duration_ms"]))
    engine_ms = [float(r["engine_ms"]) for r in records if isinstance(r.get("engine_ms"), (int, float))]
    return {
        "successful_requests": {
            "n": len(ok_ms),
            "p50_ms": _round(percentile(ok_ms, 50)),
            "p95_ms": _round(percentile(ok_ms, 95)),
            "max_ms": _round(max(ok_ms)) if ok_ms else None,
        },
        "all_requests": {
            "n": len(all_ms),
            "p50_ms": _round(percentile(all_ms, 50)),
            "p95_ms": _round(percentile(all_ms, 95)),
            "max_ms": _round(max(all_ms)) if all_ms else None,
            "note": "含失败/超时请求的真实耗时，失败不会被剔除，避免速度看起来更快。",
        },
        "failed_requests": {
            "n": len(failed_ms),
            "p50_ms": _round(percentile(failed_ms, 50)),
            "p95_ms": _round(percentile(failed_ms, 95)),
            "max_ms": _round(max(failed_ms)) if failed_ms else None,
        },
        "all_attempts": {
            "n": len(all_attempt_ms),
            "p50_ms": _round(percentile(all_attempt_ms, 50)),
            "p95_ms": _round(percentile(all_attempt_ms, 95)),
            "max_ms": _round(max(all_attempt_ms)) if all_attempt_ms else None,
        },
        "server_engine_ms": {
            "n": len(engine_ms),
            "p50_ms": _round(percentile(engine_ms, 50)),
            "p95_ms": _round(percentile(engine_ms, 95)),
            "max_ms": _round(max(engine_ms)) if engine_ms else None,
        },
    }


def _budget_block(records: list[dict[str, Any]], budget_ms: float) -> dict[str, Any]:
    """ask 子集的预算统计；错误与超预算都不隐藏。"""
    ask = [r for r in records if r["guard_replay"] == "ask"]
    legal = [
        r for r in ask if r["final_status"] == "ok" and r.get("dist_valid") and not r.get("payload_param_mismatches")
    ]
    within = [r for r in legal if float(r["duration_ms"]) <= budget_ms]
    within_correct_tool = [r for r in within if r["expected"] == "tool" and r.get("gate_result") == "tool"]
    within_wrong = [r for r in within if r["expected"] != "tool" and r.get("gate_result") == "tool"]
    over_budget = [r for r in ask if float(r["duration_ms"]) > budget_ms]
    errors = [r for r in ask if r["final_status"] != "ok"]
    return {
        "budget_ms": budget_ms,
        "ask_n": len(ask),
        "legal_n": len(legal),
        "within_budget_legal_n": len(within),
        "within_budget_legal_ratio_of_ask": _round(pct(len(within), len(ask))),
        "within_budget_legal_ratio_of_legal": _round(pct(len(within), len(legal))),
        "within_budget_correct_tool_n": len(within_correct_tool),
        "within_budget_correct_tool_ids": [r["case_id"] for r in within_correct_tool],
        "within_budget_wrong_replay_edit_n": len(within_wrong),
        "within_budget_wrong_replay_edit_ids": [r["case_id"] for r in within_wrong],
        "over_budget_n": len(over_budget),
        "over_budget_ids": [r["case_id"] for r in over_budget],
        "error_n": len(errors),
        "error_ids": [r["case_id"] for r in errors],
        "note": (
            "预算只做统计：本工具没有向产品注入 350ms deadline，也没有运行产品的异步 deadline / IME 路径。"
            "“合法”= 最终 ok、分布合法、payload 参数核对通过；超时与错误按真实耗时计入 over_budget/error，"
            "不会让速度看起来更快。"
        ),
    }


def summarize_round(
    records: list[dict[str, Any]],
    *,
    latency_note: str,
    is_repeat: bool,
    budget_ms: float,
) -> dict[str, Any]:
    n = len(records)
    ok = [r for r in records if r["final_status"] == "ok"]
    errors = [r for r in records if r["final_status"] != "ok"]
    timeouts = [r for r in records if r["final_status"] == "timeout"]
    first_attempt_errors = [r for r in records if not r["first_attempt_ok"]]
    valid = [r for r in records if r.get("dist_valid")]
    answered = [r for r in records if r.get("answer_choice") is not None]
    raw_correct = [r for r in answered if r.get("raw_correct")]
    excluded = [r for r in records if not r.get("raw_accuracy_eligible", True)]
    eligible_answered = [r for r in answered if r.get("raw_accuracy_eligible", True)]
    eligible_correct = [r for r in eligible_answered if r.get("raw_correct")]
    debug_only = [r for r in records if r["final_status"] == "ok" and not r.get("dist_valid") and r.get("debug_choice")]
    confusion: dict[str, dict[str, int]] = {}
    for r in answered:
        confusion.setdefault(str(r["expected"]), {})
        key = str(r["answer_choice"])
        confusion[str(r["expected"])][key] = confusion[str(r["expected"])].get(key, 0) + 1
    per_label: dict[str, Any] = {}
    for label in EXPECTED_LABELS:
        tp = sum(1 for r in answered if r["expected"] == label and r["answer_choice"] == label)
        fp = sum(1 for r in answered if r["expected"] != label and r["answer_choice"] == label)
        fn = sum(1 for r in answered if r["expected"] == label and r["answer_choice"] != label)
        precision = pct(tp, tp + fp)
        recall = pct(tp, tp + fn)
        f1 = None
        if precision is not None and recall is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        per_label[label] = {
            "support": sum(1 for r in records if r["expected"] == label),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
        }
    guard_unavailable = [r for r in records if r["guard_replay"] == GUARD_UNAVAILABLE]
    requested = [r for r in records if r["guard_replay"] == "ask"]
    guard_skipped = [r for r in records if r["guard_replay"] not in ("ask", GUARD_UNAVAILABLE)]
    replaced = [r for r in records if r.get("replay_action") == REPLAY_WOULD_REPLACE]
    replaced_wrong = [r for r in replaced if r["expected"] != "tool"]
    needed_tool = [r for r in records if r["expected"] == "tool"]
    needed_tool_raw = [r for r in needed_tool if r.get("answer_choice") == "tool"]
    needed_tool_gate = [r for r in needed_tool if r.get("gate_result") == "tool"]
    needed_tool_ask = [r for r in needed_tool if r["guard_replay"] == "ask"]
    needed_tool_ask_accepted = [r for r in needed_tool_ask if r.get("gate_result") == "tool"]
    needed_tool_guard_skipped = [r for r in needed_tool if r["guard_replay"] not in ("ask", GUARD_UNAVAILABLE)]
    needed_tool_unavailable = [r for r in needed_tool if r["guard_replay"] == GUARD_UNAVAILABLE]
    # 无有效判定只计 response 错误 / 分布不合法；分布合法的弱分布拒绝（gate_result=None）
    # 是产品的合法保守结果，不能算成“无有效判定”，单列 weak_rejected。
    needed_tool_no_verdict = [r for r in needed_tool_ask if r["final_status"] != "ok" or not r.get("dist_valid")]
    needed_tool_weak_rejected = [
        r for r in needed_tool_ask if r["final_status"] == "ok" and r.get("dist_valid") and r.get("gate_result") is None
    ]
    # 分布合法的非 tool 判定（person/unclear）保留原分类，单列不并入弱拒绝。
    needed_tool_rejected_non_tool = [
        r
        for r in needed_tool_ask
        if r["final_status"] == "ok" and r.get("dist_valid") and r.get("gate_result") not in (None, "tool")
    ]
    negatives = [r for r in records if r["expected"] != "tool"]
    # 分母只按 guard_replay==ask 直接取负例记录；其余分桶（weak / 无判定 / 跳过）不再相加以防重复。
    negative_ask = [r for r in negatives if r["guard_replay"] == "ask"]
    negative_effective_judged = [
        r for r in negatives if r["guard_replay"] == "ask" and r["final_status"] == "ok" and r.get("dist_valid")
    ]
    negative_effective_wrong = [r for r in negative_effective_judged if r.get("gate_result") == "tool"]
    negative_no_verdict = [
        r for r in negatives if r["guard_replay"] == "ask" and not (r["final_status"] == "ok" and r.get("dist_valid"))
    ]
    negative_judged_weak = [
        r
        for r in negatives
        if r["guard_replay"] == "ask"
        and r["final_status"] == "ok"
        and r.get("dist_valid")
        and r.get("gate_result") is None
    ]
    # 模型识别正确：分布合法、门槛给出 person/unclear 判定（弱分布拒绝 gate_result=None 不算）。
    negative_correct_abstain = [r for r in negative_effective_judged if r.get("gate_result") in ("person", "unclear")]
    negative_guard_skipped = [r for r in negatives if r["guard_replay"] not in ("ask", GUARD_UNAVAILABLE)]
    negative_unavailable = [r for r in negatives if r["guard_replay"] == GUARD_UNAVAILABLE]
    # 安全保留 = 结构保护跳过 + 合法判定拒绝（含弱分布拒绝）；不是模型识别正确。
    negative_kept_correct = [
        r for r in negatives if r.get("replay_correct") is True and r["guard_replay"] != GUARD_UNAVAILABLE
    ]
    negative_safe_keep_weak = [r for r in negatives if r.get("replay_action") == REPLAY_KEPT_WEAK]
    gate_counts = {
        "accept_tool": sum(1 for r in records if r.get("gate_decision") == "accept" and r.get("gate_result") == "tool"),
        "accept_person": sum(
            1 for r in records if r.get("gate_decision") == "accept" and r.get("gate_result") == "person"
        ),
        "accept_unclear": sum(
            1 for r in records if r.get("gate_decision") == "accept" and r.get("gate_result") == "unclear"
        ),
        "reject_weak": sum(1 for r in records if r.get("gate_decision") == "reject_weak"),
        "unusable_bad_distribution": sum(1 for r in records if r.get("gate_decision") == "unusable_bad_distribution"),
        "no_answer_error": sum(1 for r in records if r.get("gate_decision") == "no_answer_error"),
    }
    # 产品路径上的门槛计数：只取 guard_replay==ask（结构保护本会发请求）的记录，字段名与
    # 上面的全量反事实计数显式区分，避免把全局弱拒绝数当成 ask 子集数或两边相加。
    ask_records = [r for r in records if r["guard_replay"] == "ask"]
    gate_counts_ask_subset = {
        "ask_subset_n": len(ask_records),
        "ask_subset_accept_tool_n": sum(
            1 for r in ask_records if r.get("gate_decision") == "accept" and r.get("gate_result") == "tool"
        ),
        "ask_subset_accept_person_n": sum(
            1 for r in ask_records if r.get("gate_decision") == "accept" and r.get("gate_result") == "person"
        ),
        "ask_subset_accept_unclear_n": sum(
            1 for r in ask_records if r.get("gate_decision") == "accept" and r.get("gate_result") == "unclear"
        ),
        "ask_subset_reject_weak_n": sum(1 for r in ask_records if r.get("gate_decision") == "reject_weak"),
        "ask_subset_unusable_bad_distribution_n": sum(
            1 for r in ask_records if r.get("gate_decision") == "unusable_bad_distribution"
        ),
        "ask_subset_no_answer_error_n": sum(1 for r in ask_records if r.get("gate_decision") == "no_answer_error"),
    }
    product_path_available = not guard_unavailable and n > 0
    metrics: dict[str, Any] = {
        "n_cases": n,
        "responses": {
            "final_ok": len(ok),
            "final_error": len(errors),
            "final_error_rate": _round(pct(len(errors), n)),
            "timeout": len(timeouts),
            "timeout_rate": _round(pct(len(timeouts), n)),
            "first_attempt_ok": n - len(first_attempt_errors),
            "first_attempt_error_rate": _round(pct(len(first_attempt_errors), n)),
            "recovered_by_retry": sum(1 for r in records if r.get("recovered_by_retry")),
            "error_kinds": _counter(r["final_status"] for r in errors),
            "bad_distribution_n": sum(1 for r in records if r["final_status"] == "ok" and not r.get("dist_valid")),
            "debug_choice_not_counted_n": len(debug_only),
            "debug_choice_not_counted_ids": [r["case_id"] for r in debug_only],
        },
        "model_raw": {
            "answered_n": len(answered),
            "answer_coverage": _round(pct(len(answered), n)),
            "role_accuracy": _round(pct(len(eligible_correct), len(eligible_answered))),
            "role_accuracy_eligible_answered_n": len(eligible_answered),
            "role_accuracy_all_answered": _round(pct(len(raw_correct), len(answered))),
            "raw_accuracy_excluded_n": len(excluded),
            "raw_accuracy_excluded_ids": [r["case_id"] for r in excluded],
            "dist_valid_n": len(valid),
            "dist_invalid_n": sum(1 for r in records if r["final_status"] == "ok" and not r.get("dist_valid")),
            "invalid_reasons": _counter(
                str(r.get("invalid_reason")) for r in records if r["final_status"] == "ok" and not r.get("dist_valid")
            ),
            "confusion": confusion,
            "per_label": per_label,
            "negative_choice_tool_raw_n": sum(
                1 for r in answered if r["expected"] != "tool" and r["answer_choice"] == "tool"
            ),
            "negative_choice_tool_raw_ids": [
                r["case_id"] for r in answered if r["expected"] != "tool" and r["answer_choice"] == "tool"
            ],
            "unclear_expected_n": sum(1 for r in records if r["expected"] == "unclear"),
            "unclear_answered_unclear": sum(
                1 for r in answered if r["expected"] == "unclear" and r["answer_choice"] == "unclear"
            ),
            "note": "原始准确率只统计分布合法且有 choice 的回答；坏分布即使带 debug_choice 也不计正确。",
        },
        "gate_replay_0_70_0_20": {
            "scope": "all_requests_counterfactual",
            "scope_note": (
                "上方 accept_*/reject_weak/unusable_bad_distribution/no_answer_error 的计数范围是本轮全部 "
                "HTTP 记录，包含结构保护本会跳过、产品不会发出的反事实请求，因此不等于产品路径结果。"
                "产品路径请用 ask_subset_*（guard_replay==ask 子集）与 rewrite_replay，"
                "全局数与 ask 子集数不可相加。"
            ),
            "note": "全部请求反事实重放：门槛临时复用产品的保守拒绝规则，不是校准正确率。",
            "ask_subset_note": (
                "ask_subset_* 只统计 guard_replay==ask 的记录（结构保护之后产品真正会发出的请求），"
                "与上方全局反事实计数并列展示，不是它的子集加总。"
            ),
            **gate_counts,
            **gate_counts_ask_subset,
        },
        "structural_guard_replay": {
            "available": product_path_available,
            "unavailable_n": len(guard_unavailable),
            "unavailable_ids": [r["case_id"] for r in guard_unavailable],
            "unavailable_reason": (
                None if product_path_available else "产品模块未能导入，结构保护重放不可用（correctness=null）"
            ),
            "ask_n": len(requested),
            "skip_n": len(guard_skipped),
            "skip_kinds": _counter(r["guard_replay"] for r in guard_skipped),
            "note": "结构保护来自产品函数重放，不是完整流式 correction / IME 路径。",
        },
        "rewrite_replay": {
            "would_replace_n": len(replaced),
            "would_replace_correct_n": len(replaced) - len(replaced_wrong),
            "would_replace_wrong_n": len(replaced_wrong),
            "would_replace_wrong_ids": [r["case_id"] for r in replaced_wrong],
            "kept_by_guard_n": sum(1 for r in records if r.get("replay_action") == REPLAY_KEPT_BY_GUARD),
            "kept_by_gate_n": sum(
                1
                for r in records
                if r.get("replay_action") == REPLAY_KEPT_WEAK
                or str(r.get("replay_action", "")).startswith("kept_verdict_")
            ),
            "without_valid_verdict_n": sum(
                1 for r in records if r.get("replay_action") in (REPLAY_KEPT_ERROR, REPLAY_KEPT_BAD_DIST)
            ),
            "unavailable_n": len(guard_unavailable),
            "note": "这是结构保护 + 概率门槛重放结论，不是线上真实改写，也没有运行 IME。",
        },
        "expected_tool": {
            "total_n": len(needed_tool),
            "raw_choice_tool_n": len(needed_tool_raw),
            "gate_accepted_tool_counterfactual_n": len(needed_tool_gate),
            "ask_eligible_n": len(needed_tool_ask),
            "ask_eligible_accepted_n": len(needed_tool_ask_accepted),
            "ask_eligible_accepted_ids": [r["case_id"] for r in needed_tool_ask_accepted],
            "ask_eligible_not_accepted_n": len(needed_tool_ask) - len(needed_tool_ask_accepted),
            "ask_eligible_no_valid_verdict_n": len(needed_tool_no_verdict),
            "ask_eligible_no_valid_verdict_ids": [r["case_id"] for r in needed_tool_no_verdict],
            "ask_eligible_no_valid_verdict_note": (
                "只计 response 错误或分布不合法；分布合法的弱分布拒绝另计 "
                "ask_eligible_weak_rejected_n（不是无有效判定）。"
            ),
            "ask_eligible_weak_rejected_n": len(needed_tool_weak_rejected),
            "ask_eligible_weak_rejected_ids": [r["case_id"] for r in needed_tool_weak_rejected],
            "ask_eligible_rejected_non_tool_n": len(needed_tool_rejected_non_tool),
            "ask_eligible_rejected_non_tool_ids": [r["case_id"] for r in needed_tool_rejected_non_tool],
            "guard_skipped_n": len(needed_tool_guard_skipped),
            "guard_skipped_ids": [r["case_id"] for r in needed_tool_guard_skipped],
            "unavailable_n": len(needed_tool_unavailable),
            "unavailable_ids": [r["case_id"] for r in needed_tool_unavailable],
            "guard_skipped_note": "结构保护重放不发请求，因此不算“未修正错误”。",
            "note": (
                "分母分开：总 tool 数 = guard=ask 子集 + 结构保护跳过 + 不可用；"
                "ask 子集内四分：被门槛接受（重放命中改写） + 弱分布拒绝 + 非 tool 合法判定 + 无有效判定"
                "（错误/坏分布）。弱分布拒绝是产品的合法保守结果，不计入无有效判定。"
            ),
        },
        "negatives_person_unclear": {
            "n": len(negatives),
            "ask_eligible_n": len(negative_ask),
            "effectively_judged_n": len(negative_effective_judged),
            "effective_wrong_replay_edit_n": len(negative_effective_wrong),
            "effective_wrong_replay_edit_ids": [r["case_id"] for r in negative_effective_wrong],
            "judged_but_weak_rejected_n": len(negative_judged_weak),
            "judged_but_weak_rejected_ids": [r["case_id"] for r in negative_judged_weak],
            "model_correct_abstain_n": len(negative_correct_abstain),
            "model_correct_abstain_ids": [r["case_id"] for r in negative_correct_abstain],
            "no_valid_verdict_n": len(negative_no_verdict),
            "no_valid_verdict_ids": [r["case_id"] for r in negative_no_verdict],
            "guard_skipped_n": len(negative_guard_skipped),
            "unavailable_n": len(negative_unavailable),
            "unavailable_ids": [r["case_id"] for r in negative_unavailable],
            "safe_keep_n": len(negative_kept_correct),
            "kept_correct_n": len(negative_kept_correct),
            "safe_keep_weak_rejected_n": len(negative_safe_keep_weak),
            "note": (
                "ask_eligible_n 直接按 guard_replay==ask 取负例记录（= 有效判断 + 无有效判定），"
                "weak 是有效判断的合法分布子集，不得再加一次。"
                "“有效判断误改”只统计分布合法且门槛接受 tool 的负例；"
                "合法的弱分布拒绝（reject_weak）计入 safe_keep_n/kept_correct_n（= 安全保留），"
                "但那是产品保守门槛兜底，不算模型识别正确（模型识别正确只数 model_correct_abstain_n："
                "分布合法且门槛给出 person/unclear）。错误与坏分布 correctness=null，"
                "既不算模型正确弃权，也不算安全保留。"
            ),
        },
        "budget": _budget_block(records, budget_ms),
        "latency": _latency_block(records),
        "latency_note": latency_note,
        "is_repeat_of_identical_sentences": is_repeat,
    }
    assert_round_bucket_invariants(records, metrics)
    return metrics


def assert_round_bucket_invariants(records: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    """分桶互斥/可加性断言，防止把子集重复相加。

    历史缺陷：负例 ``ask_eligible_n`` 曾写成 有效判断 + 无判定 + 弱拒绝，而弱拒绝本来
    就是“有效判断（分布合法）”的子集，导致 ask 分母翻倍（61 = 46 + 25 而不是 15）。
    这里只用记录本身重新分桶，逐项与 metrics 对照，任何一项对不上就报错。
    """
    tool = [r for r in records if r["expected"] == "tool"]
    negatives = [r for r in records if r["expected"] != "tool"]
    ask = [r for r in records if r["guard_replay"] == "ask"]
    unavailable = [r for r in records if r["guard_replay"] == GUARD_UNAVAILABLE]
    guard_skipped = [r for r in records if r["guard_replay"] not in ("ask", GUARD_UNAVAILABLE)]
    guard = metrics["structural_guard_replay"]
    et = metrics["expected_tool"]
    ng = metrics["negatives_person_unclear"]
    problems: list[str] = []

    def _check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    _check(guard["ask_n"] == len(ask), f"guard ask_n={guard['ask_n']} != 记录数 {len(ask)}")
    _check(guard["skip_n"] == len(guard_skipped), f"guard skip_n={guard['skip_n']} != 记录数 {len(guard_skipped)}")
    _check(
        guard["unavailable_n"] == len(unavailable),
        f"guard unavailable_n={guard['unavailable_n']} != 记录数 {len(unavailable)}",
    )
    # ask = tool ask + 负例 ask（两个 expected 分桶互斥且合起来就是全部 ask）。
    _check(
        guard["ask_n"] == et["ask_eligible_n"] + ng["ask_eligible_n"],
        f"ask 不互斥：ask={guard['ask_n']} != tool ask={et['ask_eligible_n']} + 负例 ask={ng['ask_eligible_n']}",
    )
    for name, block, total_key, total in (
        ("expected_tool", et, "total_n", len(tool)),
        ("negatives_person_unclear", ng, "n", len(negatives)),
    ):
        parts = block["ask_eligible_n"] + block["guard_skipped_n"] + block["unavailable_n"]
        _check(block[total_key] == total, f"{name}.{total_key}={block[total_key]} != 记录数 {total}")
        _check(
            parts == total,
            f"{name} 分桶不互斥：ask+skip+unavailable={parts} != {total_key}={block[total_key]}",
        )
    # 负例 ask 子集恰好分成“有效判断（分布合法）”与“无有效判定”，weak 是前者的子集不可再加。
    _check(
        ng["effectively_judged_n"] + ng["no_valid_verdict_n"] == ng["ask_eligible_n"],
        "负例 ask 子集拆分不互斥：有效判断+无判定 != ask",
    )
    _check(
        ng["judged_but_weak_rejected_n"] <= ng["effectively_judged_n"],
        "弱拒绝不是有效判断的子集（被重复相加？）",
    )
    _check(
        ng["effective_wrong_replay_edit_n"] + ng["judged_but_weak_rejected_n"] + ng["model_correct_abstain_n"]
        == ng["effectively_judged_n"],
        "有效判断未按 门槛误改/弱拒绝/识别正确 三分（不互斥）",
    )
    _check(ng["safe_keep_n"] == ng["kept_correct_n"], "safe_keep_n 与 kept_correct_n 不一致")
    _check(
        ng["safe_keep_weak_rejected_n"] <= ng["safe_keep_n"],
        "弱拒绝安全保留数超过安全保留总数",
    )
    # tool ask 子集：被接受 + 未接受（含无有效判定）必须等于 ask。
    _check(
        et["ask_eligible_accepted_n"] + et["ask_eligible_not_accepted_n"] == et["ask_eligible_n"],
        "tool ask 子集拆分不互斥：接受+未接受 != ask",
    )
    _check(
        et["ask_eligible_no_valid_verdict_n"] <= et["ask_eligible_not_accepted_n"],
        "tool 无有效判定数超过未接受数",
    )
    # tool ask 子集四分互斥：接受 + 弱分布拒绝 + 非 tool 合法判定 + 无有效判定 == ask。
    # 历史缺陷：弱分布（gate_result=None 且分布合法）被算进 ask_eligible_no_valid_verdict_n。
    # 这里按记录重算各桶，防止只保住总数而把弱拒绝挪进无有效判定。
    tool_ask = [r for r in tool if r["guard_replay"] == "ask"]
    tool_ask_no_verdict = [r for r in tool_ask if r["final_status"] != "ok" or not r.get("dist_valid")]
    tool_ask_weak = [
        r for r in tool_ask if r["final_status"] == "ok" and r.get("dist_valid") and r.get("gate_result") is None
    ]
    tool_ask_non_tool = [
        r
        for r in tool_ask
        if r["final_status"] == "ok" and r.get("dist_valid") and r.get("gate_result") not in (None, "tool")
    ]
    _check(
        et["ask_eligible_no_valid_verdict_n"] == len(tool_ask_no_verdict),
        "tool 无有效判定计数与记录不符（弱拒绝/合法判定被算进来？）",
    )
    _check(
        et["ask_eligible_weak_rejected_n"] == len(tool_ask_weak),
        "tool 弱分布拒绝计数与记录不符",
    )
    _check(
        et["ask_eligible_rejected_non_tool_n"] == len(tool_ask_non_tool),
        "tool 非 tool 合法判定计数与记录不符",
    )
    _check(
        et["ask_eligible_accepted_n"]
        + et["ask_eligible_weak_rejected_n"]
        + et["ask_eligible_rejected_non_tool_n"]
        + et["ask_eligible_no_valid_verdict_n"]
        == et["ask_eligible_n"],
        "tool ask 子集未按 接受/弱拒绝/非tool拒绝/无有效判定 四分（不互斥）",
    )
    # 门槛重放：全量反事实计数与 ask 子集计数各有明确口径，ask 子集分桶必须自洽。
    gate_replay = metrics["gate_replay_0_70_0_20"]
    _check(
        gate_replay["scope"] == "all_requests_counterfactual",
        f"gate 重放 scope 未标明全量反事实：{gate_replay['scope']!r}",
    )
    _check(
        gate_replay["ask_subset_n"] == len(ask),
        f"gate ask_subset_n={gate_replay['ask_subset_n']} != guard ask 记录数 {len(ask)}",
    )
    _check(
        gate_replay["ask_subset_accept_tool_n"]
        + gate_replay["ask_subset_accept_person_n"]
        + gate_replay["ask_subset_accept_unclear_n"]
        + gate_replay["ask_subset_reject_weak_n"]
        + gate_replay["ask_subset_unusable_bad_distribution_n"]
        + gate_replay["ask_subset_no_answer_error_n"]
        == gate_replay["ask_subset_n"],
        "gate ask 子集分桶不互斥（接受/拒绝/错误相加 != ask_subset_n）",
    )
    _check(
        gate_replay["reject_weak"] >= gate_replay["ask_subset_reject_weak_n"],
        "ask 子集弱拒绝数超过全量反事实弱拒绝数",
    )
    # error / 坏分布（以及 guard 不可用）不得被当成模型正确弃权或安全保留。
    bad_or_error = [
        r for r in negatives if r["guard_replay"] == "ask" and not (r["final_status"] == "ok" and r.get("dist_valid"))
    ]
    _check(
        all(r.get("replay_correct") is None for r in bad_or_error),
        "error/坏分布负例被计入了正确弃权或安全保留",
    )
    _check(
        all(r.get("replay_correct") is None for r in unavailable),
        "guard_replay=unavailable 的记录不应有 correctness",
    )
    if problems:
        raise AssertionError("分桶不变量被破坏：" + "；".join(problems))


def _counter(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------
# Markdown 摘要
# --------------------------------------------------------------------------
def render_summary_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    versions = report["versions"]
    temp = report.get("temperature", {})
    integrity = report.get("payload_integrity", {})
    comparability = report.get("comparability", {})
    lines: list[str] = []
    lines.append(f"# SystemOne 语境别名对照 — {report.get('label') or 'run'}")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 端点：`{run.get('endpoint')}`（provider={run.get('provider')}）")
    lines.append(
        f"- 模型元数据：label=`{run.get('model_label') or 'unknown'}`，revision=`{run.get('model_revision') or 'unknown'}`，"
        f"quantization=`{run.get('quantization') or 'unknown'}`，响应中观测到的 model=`{run.get('model_name') or 'none'}`"
    )
    lines.append(
        f"- 温度：temperature_sent=`{temp.get('temperature_sent')}`（产品不发送），"
        f"effective=`{temp.get('effective_temperature')}`，source=`{temp.get('temperature_source')}`；"
        f"本 run 部署配置/温度已核实=`{comparability.get('this_run_release_config_verified')}`，"
        f"跨模型可比声明（单个 run 无权给出）=`{comparability.get('cross_model_gate_comparable')}`"
    )
    lines.append(
        f"- 语料：`{versions['cases_path']}` sha256=`{versions['cases_sha256']}`，"
        f"{versions['case_count']} 条（负例 {versions['negative_count']}）"
    )
    lines.append(
        f"- 工具：`{versions['tool_name']}` sha256=`{versions['tool_sha256']}`（schema {report['report_schema']}）"
    )
    lines.append(
        f"- 请求：串行、固定顺序，payload={integrity.get('payload_shape')}，timeout={run['timeout_s']}s，"
        f"retries={run['retries']}，retry_delay={run['retry_delay_s']}s，rounds={run['rounds_requested']}，"
        f"warm_new_round={run['warm_new_round']}，budget_ms={run['budget_ms']}"
    )
    lines.append(
        f"- 门槛：interpret_choice top≥{versions['gate']['min_top']} 且 margin≥{versions['gate']['min_margin']}"
        f"（来源 `{versions['gate']['source']}`）"
    )
    lines.append(
        f"- payload 完整性：主轮唯一 payload={integrity.get('main_unique_payload_sha256_n')}/{integrity.get('main_records_n')}，"
        f"alt 碰撞={len(integrity.get('alt_payload_collision_refs') or [])}，"
        f"参数核对不符={len(integrity.get('product_param_mismatch_ids') or [])}，"
        f"已声明偏离={len(integrity.get('declared_param_deviation_ids') or [])}"
    )
    lines.append("")
    lines.append(
        "> **边界**：0.70/0.20 只是产品的保守拒绝门槛，不是校准正确率；所有改写结论都是"
        "**结构保护与概率门槛重放**，本工具没有运行完整流式 correction、没有 IME、没有产品异步 deadline。"
        "HTTP 错误、超时、坏分布都不算正确弃权，单独计数。"
    )
    lines.append(">")
    if comparability.get("this_run_comparison_preconditions_met"):
        lines.append(
            f"> **本 run 一侧比较前置条件已满足（单个 run 不宣布两个 run 可比）**：{comparability.get('reason')}"
        )
    else:
        lines.append(f"> **比较前置条件未满足（effective_temperature=unknown）**：{comparability.get('reason')}")
    lines.append("")
    lines.append(
        "| 轮次 | 类型 | 句数 | 错误率 | 超时率 | 原始准确率(可用子集) | 原始准确率(全部回答) | "
        "重放改写(对/错) | tool: 总/ask子集/接受 | 预算内合法/ask |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for item in report["rounds"]:
        m = item["metrics"]
        mr = m["model_raw"]
        et = m["expected_tool"]
        rr = m["rewrite_replay"]
        b = m["budget"]
        lines.append(
            f"| {item['round_index']} | {item['round_type']} | {m['n_cases']} | "
            f"{_fmt(m['responses']['final_error_rate'])} | {_fmt(m['responses']['timeout_rate'])} | "
            f"{_fmt(mr['role_accuracy'])} ({mr['role_accuracy_eligible_answered_n']}) | "
            f"{_fmt(mr['role_accuracy_all_answered'])} | "
            f"{rr['would_replace_correct_n']}/{rr['would_replace_wrong_n']} | "
            f"{et['total_n']}/{et['ask_eligible_n']}/{et['ask_eligible_accepted_n']} | "
            f"{b['within_budget_legal_n']}/{b['ask_n']} |"
        )
    lines.append("")
    for item in report["rounds"]:
        m = item["metrics"]
        lines.append(f"## 轮次 {item['round_index']} — {item['round_type']}")
        lines.append("")
        lines.append(f"- {item['description']}")
        lines.append(f"- 延迟说明：{m['latency_note']}")
        lat_all = m["latency"]["all_requests"]
        lat_ok = m["latency"]["successful_requests"]
        lines.append(
            f"- 延迟：成功 n={lat_ok['n']} p50={_fmt(lat_ok['p50_ms'])}ms p95={_fmt(lat_ok['p95_ms'])}ms "
            f"max={_fmt(lat_ok['max_ms'])}ms；全部请求 n={lat_all['n']} p50={_fmt(lat_all['p50_ms'])}ms "
            f"p95={_fmt(lat_all['p95_ms'])}ms max={_fmt(lat_all['max_ms'])}ms（含失败/超时）"
        )
        lines.append(
            f"- 预算（ask 子集，budget={_fmt(m['budget']['budget_ms'])}ms）：合法={m['budget']['legal_n']}，"
            f"预算内合法={m['budget']['within_budget_legal_n']}（占 ask {_fmt(m['budget']['within_budget_legal_ratio_of_ask'])}，"
            f"占合法 {_fmt(m['budget']['within_budget_legal_ratio_of_legal'])}），"
            f"预算内正确热词={m['budget']['within_budget_correct_tool_n']}，"
            f"预算内重放误改={m['budget']['within_budget_wrong_replay_edit_n']}，"
            f"超预算={m['budget']['over_budget_n']}，错误={m['budget']['error_n']}"
        )
        lines.append(
            f"- 原始模型：answered={m['model_raw']['answered_n']}/{m['n_cases']}，"
            f"role accuracy(可用子集)={_fmt(m['model_raw']['role_accuracy'])}，"
            f"role accuracy(全部回答)={_fmt(m['model_raw']['role_accuracy_all_answered'])}，"
            f"分布合法={m['model_raw']['dist_valid_n']}，坏分布={m['model_raw']['dist_invalid_n']}"
            f"（带 debug_choice 但未计正确={m['responses']['debug_choice_not_counted_n']}）"
        )
        lines.append(
            f"- 原始准确率排除：{m['model_raw']['raw_accuracy_excluded_n']} 条同小句多目标结构跳过样例"
            f"{' ' + str(m['model_raw']['raw_accuracy_excluded_ids']) if m['model_raw']['raw_accuracy_excluded_ids'] else ''}"
        )
        lines.append(f"- 错误分类：{m['responses']['error_kinds'] or '{}'}")
        g = m["gate_replay_0_70_0_20"]
        lines.append(
            f"- 概率门槛重放（scope=all_requests_counterfactual，含结构保护本会跳过、产品不会发出的反事实请求，"
            f"n={m['n_cases']}）：接受 tool={g['accept_tool']}，接受 person={g['accept_person']}，"
            f"接受 unclear={g['accept_unclear']}，弱分布拒绝={g['reject_weak']}，"
            f"坏分布={g['unusable_bad_distribution']}，错误无答案={g['no_answer_error']}"
        )
        lines.append(
            f"- 概率门槛重放（guard=ask 子集，即结构保护之后产品路径，n={g['ask_subset_n']}）："
            f"接受 tool={g['ask_subset_accept_tool_n']}，接受 person={g['ask_subset_accept_person_n']}，"
            f"接受 unclear={g['ask_subset_accept_unclear_n']}，弱分布拒绝={g['ask_subset_reject_weak_n']}，"
            f"坏分布={g['ask_subset_unusable_bad_distribution_n']}，错误无答案={g['ask_subset_no_answer_error_n']}"
            "（与上一行全量反事实数不同口径，不可相加）"
        )
        guard = m["structural_guard_replay"]
        lines.append(
            f"- 结构保护重放：可用={guard['available']}，会发请求={guard['ask_n']}，跳过={guard['skip_n']} "
            f"{guard['skip_kinds']}，不可用={guard['unavailable_n']}"
        )
        rr = m["rewrite_replay"]
        lines.append(
            f"- 重放改写（结构保护+门槛）：命中={rr['would_replace_n']}，其中正确={rr['would_replace_correct_n']}，"
            f"重放误改={rr['would_replace_wrong_n']}"
            f"{' ' + str(rr['would_replace_wrong_ids']) if rr['would_replace_wrong_ids'] else ''}；"
            f"结构保护保持原文={rr['kept_by_guard_n']}，门槛保持原文={rr['kept_by_gate_n']}，"
            f"无有效判定={rr['without_valid_verdict_n']}"
        )
        et = m["expected_tool"]
        lines.append(
            f"- 需要 tool：总={et['total_n']}，原始答 tool={et['raw_choice_tool_n']}，"
            f"guard=ask 子集={et['ask_eligible_n']}，该子集被接受={et['ask_eligible_accepted_n']}"
            f"{' ' + str(et['ask_eligible_accepted_ids']) if et['ask_eligible_accepted_ids'] else ''}；"
            f"弱分布拒绝（合法，不算无有效判定）={et['ask_eligible_weak_rejected_n']}"
            f"{' ' + str(et['ask_eligible_weak_rejected_ids']) if et['ask_eligible_weak_rejected_ids'] else ''}；"
            f"非 tool 合法判定={et['ask_eligible_rejected_non_tool_n']}；"
            f"无有效判定（仅错误/坏分布）={et['ask_eligible_no_valid_verdict_n']}"
            f"{' ' + str(et['ask_eligible_no_valid_verdict_ids']) if et['ask_eligible_no_valid_verdict_ids'] else ''}；"
            f"结构保护跳过={et['guard_skipped_n']}（不算未修正错误）"
            f"{' ' + str(et['guard_skipped_ids']) if et['guard_skipped_ids'] else ''}"
        )
        ng = m["negatives_person_unclear"]
        lines.append(
            f"- person/unclear 负例：{ng['n']} 条 = guard=ask 分母 {ng['ask_eligible_n']}"
            f" + 结构保护跳过 {ng['guard_skipped_n']} + 不可用 {ng['unavailable_n']}；"
            f"ask 内有效判断={ng['effectively_judged_n']}（有效判断误改={ng['effective_wrong_replay_edit_n']}"
            f"{' ' + str(ng['effective_wrong_replay_edit_ids']) if ng['effective_wrong_replay_edit_ids'] else ''}，"
            f"模型识别正确={ng['model_correct_abstain_n']}，弱分布拒绝={ng['judged_but_weak_rejected_n']}），"
            f"无有效判定={ng['no_valid_verdict_n']}；安全保留={ng['safe_keep_n']}"
            f"（含合法弱分布拒绝 {ng['safe_keep_weak_rejected_n']}，属产品保守门槛兜底，不算模型识别正确）"
        )
        lines.append(f"- 混淆矩阵（分布合法回答）：`{json.dumps(m['model_raw']['confusion'], ensure_ascii=False)}`")
        lines.append("")
    lines.append("## 可追溯性")
    lines.append("")
    lines.append(
        f"- 候选/说明（{versions['gate']['source']}）：`{json.dumps(versions['criteria'], ensure_ascii=False)}`"
    )
    lines.append(f"- 指令模板：`{versions['instructions_template']}`")
    lines.append(f"- 状态模板：`{versions['state_template']}`")
    lines.append(
        f"- 语料计数：`{json.dumps(versions['corpus']['expected_counts'], ensure_ascii=False)}`，"
        f"guard 预判：`{json.dumps(versions['corpus']['guard_expected_counts'], ensure_ascii=False)}`"
    )
    lines.append(
        f"- payload 形状：`{integrity.get('payload_shape')}`；"
        f"surface 与词表 heard 相同的条数={versions['corpus'].get('surface_equals_lexicon_heard_n')}，"
        f"不同={len(versions['corpus'].get('surface_differs_from_lexicon_heard_ids') or [])}"
    )
    if report["versions"].get("guard_check"):
        gc = report["versions"]["guard_check"]
        lines.append(
            f"- guard 交叉核对：核对 {gc['checked']} 条，不一致 {gc['mismatch_count']} 条"
            f"{' ' + str(gc['mismatch_ids']) if gc['mismatch_ids'] else ''}（来源 {gc['source']}）"
        )
    lines.append(f"- 产品模块哈希：`{json.dumps(versions.get('product_module_hashes', {}), ensure_ascii=False)}`")
    manifest = report.get("runtime_manifest", {})
    lines.append(f"- 运行清单：path=`{manifest.get('path')}` sha256=`{manifest.get('sha256')}`")
    lines.append("")
    lines.append("## 注意事项")
    lines.append("")
    for caveat in report["caveats"]:
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通用 SystemOne choice 模型对照基准（Recordian 语境别名 role 判定，串行、固定顺序）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--endpoint", default="", help="HTTP 端点，例如 http://HOST:PORT/v1/systemone")
    parser.add_argument("--cases", default=str(Path(__file__).with_name("cases.json")), help="带标签语料 JSON")
    parser.add_argument("--output", default="systemone-decision-report.json", help="报告 JSON 输出路径")
    parser.add_argument("--summary-md", default="", help="可选：Markdown 摘要输出路径")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="单次请求超时（秒，诊断捕获）")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="总轮数：第 1 轮 cold，其后为完全重复轮")
    parser.add_argument(
        "--warm-new-round", action="store_true", help="在重复轮之后追加一轮 alt 新句子（warm 但非重复）"
    )
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="每次请求最多重试次数（首个失败仍记录）")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY_S, help="重试间隔（秒）")
    parser.add_argument("--inter-request-delay", type=float, default=0.0, help="请求间隔（秒），默认 0")
    parser.add_argument(
        "--budget-ms", type=float, default=DEFAULT_BUDGET_MS, help="ask 子集的延迟预算（毫秒，只统计不注入 deadline）"
    )
    parser.add_argument(
        "--provider", choices=("http", "jev"), default="http", help="http=SystemOne 端点；jev=官方 CLI（需显式选择）"
    )
    parser.add_argument("--jev-argv", nargs="*", default=None, help="自定义 jev 命令前缀（默认产品内的 jev ask）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟用，0=全部）")
    parser.add_argument("--shuffle", action="store_true", help="打乱顺序（默认固定顺序）")
    parser.add_argument("--seed", type=int, default=20260925, help="--shuffle 的随机种子")
    parser.add_argument("--label", default="", help="报告标签，例如 semif-baseline-v2")
    parser.add_argument("--notes", default="", help="自由备注，写进报告")
    parser.add_argument("--src-path", default="", help="recordian 源码路径（默认自动探测 ../../src）")
    parser.add_argument("--model-label", default="", help="本次运行声明的模型/revision 标签（只记录，不发送）")
    parser.add_argument("--model-revision", default="", help="模型 revision/commit（只记录，不发送）")
    parser.add_argument("--quantization", default="", help="量化（只记录，不发送）")
    parser.add_argument(
        "--effective-temperature",
        default="",
        help="经核实的有效温度；未知就留空或写 unknown（不发送，只记录）",
    )
    parser.add_argument(
        "--temperature-source",
        default="",
        help="有效温度的来源说明；只有同时给出数值与来源才算已核实，否则写 unknown 并显著标注（本 run 不满足比较前置条件）",
    )
    parser.add_argument("--runtime-manifest", default="", help="运行时清单路径（只记录路径与 sha256）")
    parser.add_argument(
        "--compat-lexicon-heard",
        action="store_true",
        help="仅复现修正前证据：instructions.heard 用词表 heard 而不是 surface（非产品形状，报告会标记）",
    )
    parser.add_argument("--validate-only", action="store_true", help="只校验语料与产品桥接，不发请求")
    parser.add_argument("--dry-run", action="store_true", help="打印前几个完整请求载荷后退出，不发请求")
    parser.add_argument("--dry-run-count", type=int, default=2, help="--dry-run 打印的请求数")
    parser.add_argument("--quiet", action="store_true", help="不打印每条进度")
    return parser.parse_args(argv)


def load_cases(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} 顶层不是 JSON 对象")
    return data


def temperature_block(args: argparse.Namespace) -> dict[str, Any]:
    raw = str(args.effective_temperature).strip()
    source = str(args.temperature_source).strip()
    value: float | str
    invalid: str | None = None
    if not raw or raw.lower() in ("unknown", "null", "none"):
        value = "unknown"
    else:
        try:
            value = float(raw)
        except ValueError:
            value = "unknown"
            invalid = raw
    known = isinstance(value, float) and bool(source) and source.lower() != "unknown"
    return {
        "temperature_sent": None,
        "temperature_sent_note": (
            "顶层 temperature/model 不发送：产品 semif_judge.request_choices 的 payload 只有 state/questions；"
            "SemIf、Winnow、Decider 对这两个顶层字段的含义各不相同，不盲目发送。"
        ),
        "effective_temperature": value if known else "unknown",
        "temperature_source": source if known else "unknown",
        "verified": known,
        "invalid_cli_value": invalid,
        "release_configuration_note": (
            "每个模型按各自发布配置（含各自发布温度）对照实际产品行为，不强制统一温度；"
            "温度不同不禁止对照实际接受/误改/延迟，但概率值不能跨模型当作相同真实置信度，"
            "相同温度也不意味着概率已校准。因此这不是校准精度比较。"
        ),
    }


def comparability_block(temperature: dict[str, Any]) -> dict[str, Any]:
    """可比性前置条件：只核实本 run 一侧，不代替其他 run 的核实，也不由单个 run 宣布可比。"""
    verified = bool(temperature.get("verified"))
    if verified:
        reason = (
            f"本 run 的部署配置与有效温度已核实（{temperature.get('effective_temperature')}，来源 "
            f"{temperature.get('temperature_source')}）；按各自发布配置对照重放行为时，本 run 一侧前置条件已满足。"
            "单个 run 不能判定两个 run 已经可比：其他 run 必须各自核实部署配置与发布温度，"
            "且对照需同一 cases sha256 / 同一 payload 形状 / 同一门槛。温度不同不构成禁止比较的理由，"
            "实际接受/误改/延迟可以按各自发布配置对照；但概率值不能跨模型当作相同真实置信度，"
            "相同温度也不意味着不同模型的概率已校准。"
        )
        scope = "this_run_preconditions_met; each_other_run_must_be_verified_separately"
    else:
        reason = (
            "本 run 的 effective_temperature 为 unknown：0.70/0.20 门槛作用在概率分布上，会随服务端有效温度变化；"
            "在核实该模型部署配置与发布温度之前，本 run 不满足比较前置条件（unknown 必须显著标注）。"
        )
        scope = "blocked_unknown_effective_temperature"
    return {
        "cross_model_gate_comparable": False,
        "cross_model_gate_comparable_scope": scope,
        "cross_model_gate_comparable_note": (
            "单个 run 无权宣布两个 run 可比，故恒为 false；本 run 一侧是否满足前置条件看 "
            "this_run_comparison_preconditions_met。"
        ),
        "this_run_release_config_verified": verified,
        "this_run_comparison_preconditions_met": verified,
        "reason": reason,
        "requirements": (
            "对照重放行为（实际接受/误改/延迟）需：同一 cases sha256、同一 payload 形状、同一门槛，"
            "并且每个被比较 run 都各自核实了部署配置与发布温度；各自发布温度可以不同。"
        ),
        "release_temperature_reference": {
            "semif_1_0": 1.0,
            "winnow_12b": 1.0,
            "decider_4b_v2": 1.935,
            "note": (
                "官方发布配置举例（SemIf：softmax(raw logits)，无温度缩放=1.0；Winnow release-manifest 校准 1.0；"
                "Decider decider_config.json 1.935）。不同温度不是禁止比较的理由，但必须写明；"
                "SemIf 1.0 / Winnow 1.0 / Decider 1.935 之间可以对照实际接受/误改/延迟；"
                "概率值不能跨模型当作相同真实置信度，相同温度也不意味着概率已校准。"
            ),
        },
        "is_calibration_accuracy_comparison": False,
    }


def comparability_caveats(comparability: dict[str, Any]) -> list[str]:
    """温度/可比性口径的注意事项；run 与离线重算共用，保证文案一致。"""
    if comparability.get("this_run_comparison_preconditions_met"):
        return [
            "本 run 的部署配置与有效温度已核实，满足“按各自发布配置对照重放行为”的本 run 一侧前置条件；"
            "对照其他 run 还要求同一 cases sha256 / 同一 payload 形状 / 同一门槛，且每个 run 各自核实过发布温度。"
            "单个 run 不能判定两个 run 已经可比。各模型按各自发布温度对照实际接受/误改/延迟；"
            "概率值不能跨模型当作相同真实置信度，相同温度也不意味着概率已校准。",
        ]
    return [
        "effective_temperature=unknown：该模型部署配置与发布温度未核实，本 run 不满足比较前置条件，"
        "报告显著标注 unknown；先核实温度，再按各自发布配置对照重放行为。",
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bridge = ProductBridge(args.src_path or None)
    cases_path = Path(args.cases).resolve()
    cases_data = load_cases(cases_path)
    problems, corpus_summary = validate_corpus(
        cases_data,
        cases_path,
        bridge=bridge,
        compat_lexicon_heard=bool(args.compat_lexicon_heard),
    )
    alias = corpus_summary["alias"]
    cases: list[dict[str, Any]] = list(cases_data["cases"])
    payload_shape = str(corpus_summary["payload_shape"])

    guard_check: dict[str, Any] = {"source": bridge.source, "checked": 0, "mismatch_count": 0, "mismatch_ids": []}
    if bridge.product_path_available:
        for case in cases:
            span = case["target_span"]
            actual = bridge.guard_reason(
                str(case["text"]), int(span["start"]), int(span["end"]), str(case.get("context", "") or ""), alias
            )
            declared = str(case.get("guard_expected", "unknown"))
            guard_check["checked"] += 1
            if actual != declared:
                guard_check["mismatch_count"] += 1
                guard_check["mismatch_ids"].append(str(case["id"]))
            case["_guard_replay"] = actual
    for case in cases:
        case.setdefault("_guard_replay", GUARD_UNAVAILABLE)

    if args.validate_only:
        print(
            f"语料：{corpus_summary['case_count']} 条（负例 {corpus_summary['negative_count']}，"
            f"alt {corpus_summary['alt_count']}）"
        )
        print(f"expected：{corpus_summary['expected_counts']}")
        print(f"guard 预判：{corpus_summary['guard_expected_counts']}")
        print(f"分类：{corpus_summary['categories']}")
        print(f"payload 形状：{payload_shape}")
        print(
            f"payload 完整性：主轮唯一 {corpus_summary['main_unique_payload_sha256_n']}/{corpus_summary['case_count']}，"
            f"alt 唯一 {corpus_summary['alt_unique_payload_sha256_n']}/{corpus_summary['alt_count']}，"
            f"主轮重复 {corpus_summary['main_duplicate_payload_refs'] or '无'}，"
            f"alt 碰撞 {corpus_summary['alt_payload_collision_refs'] or '无'}"
        )
        print(
            f"surface≠词表 heard：{len(corpus_summary['surface_differs_from_lexicon_heard_ids'])} 条"
            f"（heard=surface 与产品 _judge 一致）"
        )
        print(f"原始准确率排除（同小句多目标）：{corpus_summary['raw_accuracy_excluded_ids']}")
        if corpus_summary["declared_param_deviation_ids"]:
            print(
                f"已声明参数偏离（legacy 模式，非产品形状）："
                f"{len(corpus_summary['declared_param_deviation_ids'])} 条 "
                f"heard_is_surface_like_product_judge"
            )
        print(f"产品桥接：{bridge.source}" + (f"（{bridge.import_error}）" if bridge.import_error else ""))
        if not bridge.product_path_available:
            print("产品模块不可用：结构保护重放将标记 unavailable，correctness=null（不会报零误改）。")
        if guard_check["mismatch_count"]:
            print(f"guard 交叉核对不一致 {guard_check['mismatch_count']} 条：{guard_check['mismatch_ids']}")
        elif bridge.product_path_available:
            print(f"guard 交叉核对：{guard_check['checked']} 条全部一致")
        if problems:
            print("语料问题：")
            for problem in problems:
                print(f"  - {problem}")
            return 2
        print("语料校验通过，未发请求（--validate-only）。")
        return 0

    if problems:
        print("语料校验失败，未发请求：", file=sys.stderr)
        for problem in problems[:50]:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if args.provider == "http" and not args.endpoint:
        print("--provider http 需要 --endpoint", file=sys.stderr)
        return 2
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]
    if args.shuffle:
        random.Random(args.seed).shuffle(cases)

    temperature = temperature_block(args)
    comparability = comparability_block(temperature)
    manifest_path = str(args.runtime_manifest).strip()
    runtime_manifest = {
        "path": manifest_path or None,
        "sha256": sha256_file(Path(manifest_path)) if manifest_path else None,
        "note": (
            "只记录路径与 sha256，不复制内容；未提供为 null。"
            "示例：Winnow 官方校验清单 release-manifest.json（manifest 1.0）。"
        ),
    }
    budget_ms = max(0.0, float(args.budget_ms))

    if args.dry_run:
        print(
            f"provider={args.provider} endpoint={args.endpoint or '(jev CLI)'} cases={len(cases)} budget_ms={budget_ms}"
        )
        print(f"criteria={json.dumps(bridge.criteria, ensure_ascii=False)}")
        for case in cases[: max(0, args.dry_run_count)]:
            built = build_request(
                case, bridge=bridge, alias=alias, compat_lexicon_heard=bool(args.compat_lexicon_heard)
            )
            print("---")
            print(f"case={case['id']} expected={case['expected']} guard_replay={case['_guard_replay']}")
            print(json.dumps(built["payload"], ensure_ascii=False, indent=2))
            print(f"payload_sha256={built['payload_sha256']} param_mismatches={built['param_mismatches']}")
        print("（dry-run，未发请求）")
        return 0

    transport: HttpTransport | JevTransport
    if args.provider == "jev":
        transport = JevTransport(bridge, max(0.05, args.timeout), args.jev_argv)
    else:
        transport = HttpTransport(
            args.endpoint, max(0.01, args.timeout), user_agent=f"systemone-decision-benchmark/{REPORT_SCHEMA}"
        )

    rounds: list[dict[str, Any]] = []
    total_rounds = max(1, args.rounds)
    model_name: str | None = None
    started_at = now_iso()
    run_started = time.perf_counter()
    try:
        round_plan: list[tuple[str, str]] = []
        for index in range(1, total_rounds + 1):
            if index == 1:
                round_plan.append(
                    (
                        "cold_unique",
                        "第 1 轮：每句只出现一次，顺序固定，串行；服务可能已被别的调用预热，故称 cold-ish。",
                    )
                )
            else:
                round_plan.append(
                    (
                        "repeat_identical",
                        "完全重复轮：句子与第 1 轮逐字相同，只用于看重复/缓存效应；这里的延迟不代表新句子的延迟。",
                    )
                )
        if args.warm_new_round:
            round_plan.append(
                (
                    "warm_new",
                    "warm 新句子轮：使用每条的 alt 改写句（payload 与 cold 轮不同），服务已热，"
                    "这才是接近新句子的延迟参考。",
                )
            )
        for round_index, (round_type, description) in enumerate(round_plan, start=1):
            records: list[dict[str, Any]] = []
            for case in cases:
                if round_type == "warm_new":
                    alt = case.get("alt")
                    if not isinstance(alt, dict):
                        continue
                    request_case: dict[str, Any] = {
                        "id": f"{case['id']}#alt",
                        "category": case.get("category", "?"),
                        "text": alt["text"],
                        "focus": alt["focus"],
                        "context": case.get("context", ""),
                        "target_span": alt["target_span"],
                        "expected": case["expected"],
                    }
                    source = "alt"
                    guard_replay = bridge.guard_reason(
                        str(alt["text"]),
                        int(alt["target_span"]["start"]),
                        int(alt["target_span"]["end"]),
                        str(case.get("context", "") or ""),
                        alias,
                    )
                else:
                    request_case = case
                    source = "main"
                    guard_replay = str(case["_guard_replay"])
                record = run_case(
                    transport,
                    bridge,
                    alias,
                    request_case,
                    source=source,
                    retries=max(0, args.retries),
                    retry_delay_s=max(0.0, args.retry_delay),
                    guard_replay=guard_replay,
                    compat_lexicon_heard=bool(args.compat_lexicon_heard),
                )
                if model_name is None and record.get("model"):
                    model_name = str(record["model"])
                records.append(record)
                if not args.quiet:
                    print(
                        f"[r{round_index}:{round_type}] {record['case_id']} expected={record['expected']} "
                        f"answer={record.get('answer_choice')} debug_choice={record.get('debug_choice')} "
                        f"gate={record.get('gate_result')} guard={record['guard_replay']} "
                        f"status={record['final_status']} {record['duration_ms']}ms",
                        flush=True,
                    )
                if args.inter_request_delay > 0:
                    time.sleep(args.inter_request_delay)
            is_repeat = round_type == "repeat_identical"
            note = (
                "完全重复句：延迟受服务端缓存/前缀复用影响，不能当作新句子的速度。"
                if is_repeat
                else "每句唯一，cold-ish：服务可能已被其他调用预热。"
                if round_type == "cold_unique"
                else "alt 新句子 + 服务已热：新句子延迟参考。"
            )
            metrics = summarize_round(records, latency_note=note, is_repeat=is_repeat, budget_ms=budget_ms)
            rounds.append(
                {
                    "round_index": round_index,
                    "round_type": round_type,
                    "description": description,
                    "n_cases": len(records),
                    "metrics": metrics,
                    "records": records,
                }
            )
            print(
                f"== 轮次 {round_index} ({round_type}) 完成：n={len(records)} "
                f"错误率={_fmt(metrics['responses']['final_error_rate'])} "
                f"原始准确率={_fmt(metrics['model_raw']['role_accuracy'])} "
                f"重放改写={metrics['rewrite_replay']['would_replace_n']} "
                f"预算内合法={metrics['budget']['within_budget_legal_n']}/{metrics['budget']['ask_n']}",
                flush=True,
            )
    finally:
        transport.close()

    finished_at = now_iso()
    payload_integrity = {
        "payload_shape": payload_shape,
        "payload_shape_note": (
            "product_judge_heard_surface：与 streaming_correction._judge 一致（默认）。"
            "legacy_lexicon_heard：只用 --compat-lexicon-heard 复现修正前证据，不是产品请求形状。"
        ),
        "request_payloads_are_synthetic": True,
        "request_payloads_no_private_text": True,
        "main_records_n": corpus_summary["case_count"],
        "main_unique_payload_sha256_n": corpus_summary["main_unique_payload_sha256_n"],
        "main_duplicate_payload_refs": corpus_summary["main_duplicate_payload_refs"],
        "alt_records_n": corpus_summary["alt_count"],
        "alt_unique_payload_sha256_n": corpus_summary["alt_unique_payload_sha256_n"],
        "alt_payload_collision_refs": corpus_summary["alt_payload_collision_refs"],
        "product_param_mismatch_ids": corpus_summary["product_param_mismatch_ids"],
        "declared_param_deviation_ids": corpus_summary["declared_param_deviation_ids"],
        "surface_equals_lexicon_heard_n": corpus_summary["surface_equals_lexicon_heard_n"],
        "surface_differs_from_lexicon_heard_ids": corpus_summary["surface_differs_from_lexicon_heard_ids"],
        "same_clause_multi_target_ids": corpus_summary["same_clause_multi_target_ids"],
        "raw_accuracy_excluded_ids": corpus_summary["raw_accuracy_excluded_ids"],
        "per_record_payload_sha256_available": True,
    }
    caveats = [
        "0.70/0.20 门槛仅临时复用 Recordian interpret_choice，用于重放“产品代码路径会不会改写”，不是校准正确率。",
        "所有改写/误改数字都是**结构保护与概率门槛重放**，不是线上真实改写；本工具没有运行完整流式 correction、没有 IME、没有产品异步 deadline。",
        "顶层 temperature/model 不发送（产品 request_choices 也不发送）；部署配置与发布温度逐模型核实，"
        "未核实写 unknown 并显著标注（该 run 不满足比较前置条件）。",
        "HTTP 错误、超时、坏分布都不算“正确弃权”；它们分别计入 error / unusable_bad_distribution，带 debug_choice 也不计原始准确率。",
        "结构保护跳过的 tool 句子不算“未修正错误”，只计入 guard_skipped；原始准确率分母与产品是否发问分开报告。",
        "同小句内同一别名出现多次、无法明确指向的结构跳过样例不纳入原始 role 准确率，但仍保留结构保护重放测试。",
        "repeat_identical 轮的延迟包含服务端重复句缓存/前缀复用效应，不能当作新句子延迟。",
        "round 1 是 cold-ish unique：句子互不重复，但服务可能已被其他调用预热。",
        "expected 标签是人工为 marker 标出的那一次出现设计的；否定/元话语/引用仍按“所指对象”标注，产品是否改写由结构保护单独决定。",
        "“门槛接受(反事实)”只表示概率门槛会接受 tool；若该次出现同时命中结构保护，产品代码路径不会发请求，也就不会有改写重放结论。",
        "数字、端口、网址由本机规则独立处理，不纳入模型能力评估。",
        "语料全部是合成文本，不含任何真实录音、私密文本或账号信息；每条记录都保存完整 request_payload 与规范 sha256 供复核。",
        "本工具只发 role 类 choice 请求，与产品单句判定形状一致；不覆盖流式、缓存、取消等产品时序行为。",
        "--budget-ms 只做统计，不注入 deadline；3s timeout 是诊断捕获，不是产品 deadline。",
    ]
    if not bridge.product_path_available:
        caveats.append(
            "产品模块未能导入：结构保护重放标记 unavailable，所有重放 correctness=null；本报告不得据此宣称“零误改”。"
        )
    if payload_shape == PAYLOAD_SHAPE_LEGACY:
        caveats.append("本 run 用了 --compat-lexicon-heard：instructions.heard 不是产品形状，只能用于复现修正前证据。")
    caveats.extend(comparability_caveats(comparability))

    report: dict[str, Any] = {
        "report_schema": REPORT_SCHEMA,
        "label": args.label,
        "generated_at": finished_at,
        "notes": args.notes,
        "run": {
            "started_at": started_at,
            "finished_at": finished_at,
            "wall_seconds": round(time.perf_counter() - run_started, 3),
            "provider": args.provider,
            "endpoint": args.endpoint if args.provider == "http" else "(jev CLI)",
            "model_name": model_name,
            "model_label": str(args.model_label).strip() or None,
            "model_revision": str(args.model_revision).strip() or None,
            "quantization": str(args.quantization).strip() or None,
            "timeout_s": args.timeout,
            "budget_ms": budget_ms,
            "rounds_requested": total_rounds,
            "warm_new_round": bool(args.warm_new_round),
            "retries": max(0, args.retries),
            "retry_delay_s": args.retry_delay,
            "inter_request_delay_s": args.inter_request_delay,
            "order": f"shuffled_seed_{args.seed}" if args.shuffle else "fixed_serial",
            "limit": args.limit,
            "transport": (
                "requests"
                if (requests is not None and args.provider == "http")
                else ("urllib" if args.provider == "http" else "jev-cli")
            ),
            "payload_sent_fields": ["state", "questions"],
            "top_level_temperature_or_model_sent": False,
        },
        "temperature": temperature,
        "comparability": comparability,
        "runtime_manifest": runtime_manifest,
        "payload_integrity": payload_integrity,
        "versions": {
            "tool_name": Path(__file__).name,
            "tool_sha256": sha256_file(Path(__file__).resolve()),
            "cases_path": str(cases_path),
            "cases_sha256": corpus_summary["sha256"],
            "cases_schema_version": cases_data.get("schema_version"),
            "case_count": corpus_summary["case_count"],
            "negative_count": corpus_summary["negative_count"],
            "corpus": corpus_summary,
            "alias": alias,
            "criteria": bridge.criteria,
            "criteria_source": bridge.source,
            "instructions_template": bridge.instructions_template,
            "state_template": "上一段：{context}\\n{focus}\\n只判断这个跨度：「{surface}」",
            "gate": {
                "source": bridge.gate_source,
                "min_top": bridge.min_top,
                "min_margin": bridge.min_margin,
                "note": "临时复用产品的保守拒绝门槛；不是校准正确率，也不代表模型概率是真实置信度。",
            },
            "product_module_hashes": bridge.module_hashes,
            "guard_check": guard_check,
            "product_path_available": bridge.product_path_available,
            "product_import_error": bridge.import_error,
        },
        "rounds": rounds,
        "caveats": caveats,
        "summary_markdown": "",
    }
    report["summary_markdown"] = render_summary_markdown(report)

    output_path = Path(args.output)
    if output_path.parent and not output_path.parent.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.summary_md:
        md_path = Path(args.summary_md)
        if md_path.parent and not md_path.parent.exists():
            md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(report["summary_markdown"], encoding="utf-8")
    print(report["summary_markdown"])
    print(f"报告已写入 {output_path}" + (f" 和 {args.summary_md}" if args.summary_md else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
