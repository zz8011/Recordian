#!/usr/bin/env python3
"""decision_trial 基准的小规模本地检查（不发网络请求）。

覆盖：F1（heard=surface / 完整 payload / 产品参数逐项一致）、F2（不发送顶层
temperature/model、坏分布不计原始正确、元数据与可比性标志）、F3（主轮 payload
唯一、alt 不撞主轮、同小句多目标排除）、F4（导入失败/unknown guard →
correctness=null 且 guard_replay=unavailable）、预算超限不计成功纠词；
r4：负例 ask 分母按记录直接计数（weak 不重复相加）、分桶互斥不变量、
安全保留≠模型识别正确、比较前置条件不要求同温度。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import benchmark as bm  # noqa: E402

CASES_PATH = HERE.parent / "cases.json"


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bridge() -> bm.ProductBridge:
    return bm.ProductBridge(None)


@pytest.fixture(scope="module")
def alias(corpus: dict) -> dict:
    return corpus["alias"]


def _case(corpus: dict, case_id: str) -> dict:
    return next(case for case in corpus["cases"] if case["id"] == case_id)


def _ok_body(choice: str, probabilities: dict[str, float] | None) -> dict:
    answer: dict = {"choice": choice}
    if probabilities is not None:
        answer["probabilities"] = probabilities
    return {"answers": {"role": answer}}


class RecordingTransport:
    """记录发出的 (state, questions)，按时延/状态返回预置结果。"""

    def __init__(self, results: list[dict] | None = None, default: dict | None = None) -> None:
        self.sent: list[dict] = []
        self.results = list(results or [])
        self.default = default

    def send(self, state: str, questions: dict) -> dict:
        self.sent.append({"state": state, "questions": questions})
        if self.results:
            return self.results.pop(0)
        assert self.default is not None
        return self.default

    def close(self) -> None:
        return None


def _attempt(status: str, elapsed_s: float, body: dict | None = None) -> dict:
    return bm._attempt(status, time.perf_counter() - elapsed_s, body=body)


# ---------------------------------------------------------------- F1
def test_f1_instructions_use_surface_not_lexicon_heard(bridge, alias, corpus) -> None:
    case = _case(corpus, "person-discuss-07")  # surface = Jeff
    built = bm.build_request(case, bridge=bridge, alias=alias)
    assert built["product_params"]["heard"] == "Jeff"
    assert built["param_mismatches"] == []
    assert "误写成 Jeff" in built["instructions"]
    assert "误写成 jeff" not in built["instructions"]
    legacy = bm.build_request(case, bridge=bridge, alias=alias, compat_lexicon_heard=True)
    assert legacy["payload_shape"] == bm.PAYLOAD_SHAPE_LEGACY
    assert legacy["payload_sha256"] != built["payload_sha256"]
    assert "heard_is_surface_like_product_judge" in legacy["param_mismatches"]


def test_f1_corpus_reports_35_surface_mismatches(corpus, bridge, tmp_path) -> None:
    problems, summary = bm.validate_corpus(corpus, CASES_PATH, bridge=bridge)
    assert problems == []
    assert summary["surface_equals_lexicon_heard_n"] == 64
    assert len(summary["surface_differs_from_lexicon_heard_ids"]) == 35


def test_f1_legacy_heard_is_a_declared_deviation_not_a_blocker(corpus, bridge) -> None:
    problems, summary = bm.validate_corpus(corpus, CASES_PATH, bridge=bridge, compat_lexicon_heard=True)
    assert problems == []
    assert summary["product_param_mismatch_ids"] == []
    assert len(summary["declared_param_deviation_ids"]) == 198  # 99 main + 99 alt
    case = _case(corpus, "person-discuss-07")
    legacy = bm.build_request(case, bridge=bridge, alias=corpus["alias"], compat_lexicon_heard=True)
    assert legacy["param_mismatches"] == ["heard_is_surface_like_product_judge"]
    assert legacy["param_mismatches_blocking"] == []
    assert legacy["param_deviations"] == ["heard_is_surface_like_product_judge"]


def test_f1_payload_sha256_is_canonical(bridge, alias, corpus) -> None:
    built = bm.build_request(_case(corpus, "tool-install-01"), bridge=bridge, alias=alias)
    reordered = {"questions": built["payload"]["questions"], "state": built["payload"]["state"]}
    assert built["payload_sha256"] == bm.canonical_sha256(reordered)
    assert built["payload_sha256"] == bm.sha256_text(bm.canonical_json(built["payload"]))


# ---------------------------------------------------------------- F2
def test_f2_http_payload_has_no_top_level_temperature_or_model(bridge, alias, corpus) -> None:
    captured: dict = {}

    class StubResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return _ok_body("tool", {"tool": 0.9, "person": 0.05, "unclear": 0.05})

    class StubSession:
        @staticmethod
        def post(endpoint: str, json: dict, timeout: float) -> StubResponse:
            captured.update(json)
            return StubResponse()

    transport = bm.HttpTransport("http://example.invalid/v1/systemone", 1.0, "test-agent")
    transport.session = StubSession()
    built = bm.build_request(_case(corpus, "tool-install-01"), bridge=bridge, alias=alias)
    outcome = transport.send(str(built["state"]), built["questions"])
    assert outcome["status"] == "ok"
    assert set(captured) == {"state", "questions"}
    assert "temperature" not in captured and "model" not in captured
    assert set(captured["questions"]) == {"role"}


def test_f2_invalid_probabilities_never_count_as_correct(bridge, alias, corpus) -> None:
    case = _case(corpus, "tool-install-01")
    transport = RecordingTransport(default=_attempt("ok", 0.05, _ok_body("tool", None)))
    record = bm.run_case(
        transport, bridge, alias, case, source="main", retries=0, retry_delay_s=0.0, guard_replay="ask"
    )
    assert record["dist_valid"] is False
    assert record["answer_choice"] is None
    assert record["debug_choice"] == "tool"
    assert record["raw_correct"] is None
    assert record["gate_result"] is None
    metrics = bm.summarize_round([record], latency_note="t", is_repeat=False, budget_ms=350.0)
    assert metrics["model_raw"]["role_accuracy"] is None
    assert metrics["responses"]["bad_distribution_n"] == 1
    assert metrics["responses"]["debug_choice_not_counted_n"] == 1
    assert metrics["gate_replay_0_70_0_20"]["unusable_bad_distribution"] == 1


def test_f2_temperature_unknown_blocks_cross_model_claim() -> None:
    class Args:
        effective_temperature = ""
        temperature_source = ""

    temp = bm.temperature_block(Args())
    assert temp["temperature_sent"] is None
    assert temp["effective_temperature"] == "unknown"
    assert temp["verified"] is False
    comp = bm.comparability_block(temp)
    assert comp["this_run_release_config_verified"] is False
    assert comp["this_run_comparison_preconditions_met"] is False
    assert comp["cross_model_gate_comparable"] is False
    assert "unknown" in comp["reason"]

    class Args2:
        effective_temperature = "1.935"
        temperature_source = "official decider_config.json"

    temp2 = bm.temperature_block(Args2())
    assert temp2["effective_temperature"] == 1.935
    assert temp2["verified"] is True
    comp2 = bm.comparability_block(temp2)
    assert comp2["this_run_release_config_verified"] is True
    assert comp2["this_run_comparison_preconditions_met"] is True
    # 单个 run 只能声明自己一侧已核实，不能判定两个 run 已经可比
    assert comp2["cross_model_gate_comparable"] is False
    assert comp2["cross_model_gate_comparable_note"]

    class Args3:  # 有数值没来源：仍然不算核实
        effective_temperature = "1.0"
        temperature_source = ""

    assert bm.temperature_block(Args3())["verified"] is False


def test_no_same_temperature_requirement_residue() -> None:
    """r4：比较前置条件不要求各模型温度数值相同，也不再限制“只能比 argmax”。"""
    for name in ("benchmark.py", "README.md"):
        text = (HERE.parent / name).read_text(encoding="utf-8")
        for phrase in ("温度相同", "数值相同", "只能比较原始 argmax"):
            assert phrase not in text, f"{name} 仍有同温度限制残留：{phrase}"

    class Args:
        effective_temperature = "1.0"
        temperature_source = "read-only inspection: serve.py softmax(raw logits), no scaling"

    comp = bm.comparability_block(bm.temperature_block(Args()))
    assert "各自发布温度可以不同" in comp["requirements"]
    assert comp["cross_model_gate_comparable"] is False
    assert "不同温度不是禁止比较的理由" in comp["release_temperature_reference"]["note"]


# ---------------------------------------------------------------- F3
def test_f3_corpus_has_unique_main_and_alt_payloads(corpus, bridge) -> None:
    problems, summary = bm.validate_corpus(corpus, CASES_PATH, bridge=bridge)
    assert problems == []
    assert summary["main_unique_payload_sha256_n"] == summary["case_count"] == 99
    assert summary["alt_unique_payload_sha256_n"] == summary["alt_count"] == 99
    assert summary["main_duplicate_payload_refs"] == []
    assert summary["alt_payload_collision_refs"] == []


def test_f3_duplicate_main_payload_is_rejected(corpus, bridge) -> None:
    mutated = json.loads(json.dumps(corpus))
    case = next(c for c in mutated["cases"] if c["id"] == "multi-03")
    case["text"] = "jeff"  # 回到修正前：与 noc-01 的空上下文 focus 相同
    case["marker_text"] = "⟦jeff⟧"
    case["target_span"] = {"start": 0, "end": 4, "surface": "jeff"}
    case["occurrences"] = [{"start": 0, "end": 4, "surface": "jeff"}]
    case["focus"] = "jeff"
    problems, summary = bm.validate_corpus(mutated, CASES_PATH, bridge=bridge)
    assert any("主轮 payload 与" in problem for problem in problems)
    assert summary["main_duplicate_payload_refs"]


def test_f3_alt_payload_colliding_with_main_is_rejected(corpus, bridge) -> None:
    mutated = json.loads(json.dumps(corpus))
    case = next(c for c in mutated["cases"] if c["id"] == "mixed-08")
    case["alt"]["text"] = case["text"]
    case["alt"]["target_span"] = case["target_span"]
    case["alt"]["focus"] = case["focus"]
    problems, summary = bm.validate_corpus(mutated, CASES_PATH, bridge=bridge)
    assert any("alt payload 与主轮 payload" in problem for problem in problems)
    assert summary["alt_payload_collision_refs"]


def test_f3_multi_target_skips_excluded_from_raw_accuracy(corpus, bridge) -> None:
    _, summary = bm.validate_corpus(corpus, CASES_PATH, bridge=bridge)
    assert summary["raw_accuracy_excluded_ids"] == ["mixed-05", "mixed-06", "guard-repeat-01"]
    assert summary["same_clause_multi_target_ids"] == summary["raw_accuracy_excluded_ids"]
    case = _case(corpus, "mixed-05")
    assert case["exclude_from_raw_accuracy"] is True
    assert case["guard_expected"] == "skip_repeat_in_clause"


# ---------------------------------------------------------------- F4
def test_f4_unavailable_guard_gives_null_correctness(bridge, alias, corpus) -> None:
    broken = bm.ProductBridge(None)
    broken.source = "fallback_vendored"
    broken._alias_spans = None  # type: ignore[attr-defined]
    assert broken.product_path_available is False
    assert broken.guard_reason("jeff 安装好了", 0, 4, "", alias) == bm.GUARD_UNAVAILABLE
    case = _case(corpus, "tool-install-01")
    transport = RecordingTransport(
        default=_attempt("ok", 0.05, _ok_body("tool", {"tool": 0.9, "person": 0.05, "unclear": 0.05}))
    )
    record = bm.run_case(
        transport,
        broken,
        alias,
        case,
        source="main",
        retries=0,
        retry_delay_s=0.0,
        guard_replay=bm.GUARD_UNAVAILABLE,
    )
    assert record["replay_action"] == bm.REPLAY_UNAVAILABLE
    assert record["replay_correct"] is None
    assert record["guard_replay_available"] is False
    metrics = bm.summarize_round([record], latency_note="t", is_repeat=False, budget_ms=350.0)
    guard = metrics["structural_guard_replay"]
    assert guard["available"] is False
    assert guard["unavailable_n"] == 1
    assert metrics["rewrite_replay"]["unavailable_n"] == 1
    assert metrics["negatives_person_unclear"]["kept_correct_n"] == 0


# ---------------------------------------------------------------- 预算与计数口径
def test_budget_over_limit_is_not_a_successful_correction(bridge, alias, corpus) -> None:
    cases = [_case(corpus, "tool-install-01")]
    slow = RecordingTransport(
        default=_attempt("ok", 0.5, _ok_body("tool", {"tool": 0.9, "person": 0.05, "unclear": 0.05}))
    )
    record = bm.run_case(slow, bridge, alias, cases[0], source="main", retries=0, retry_delay_s=0.0, guard_replay="ask")
    assert record["gate_result"] == "tool"
    metrics = bm.summarize_round([record], latency_note="t", is_repeat=False, budget_ms=350.0)
    budget = metrics["budget"]
    assert record["duration_ms"] > 350.0
    assert budget["within_budget_legal_n"] == 0
    assert budget["within_budget_correct_tool_n"] == 0
    assert budget["over_budget_n"] == 1
    assert budget["over_budget_ids"] == ["tool-install-01"]


def test_timeout_counts_in_all_requests_and_never_shrinks_latency(bridge, alias, corpus) -> None:
    case = _case(corpus, "tool-install-01")
    transport = RecordingTransport(default=_attempt("timeout", 3.0))
    record = bm.run_case(
        transport, bridge, alias, case, source="main", retries=0, retry_delay_s=0.0, guard_replay="ask"
    )
    metrics = bm.summarize_round([record], latency_note="t", is_repeat=False, budget_ms=350.0)
    latency = metrics["latency"]
    assert latency["successful_requests"]["n"] == 0
    assert latency["all_requests"]["n"] == 1
    assert latency["failed_requests"]["n"] == 1
    assert latency["all_requests"]["p50_ms"] > 350.0
    assert metrics["responses"]["timeout"] == 1
    assert metrics["budget"]["error_n"] == 1
    assert metrics["budget"]["within_budget_legal_n"] == 0


def test_guard_skipped_tool_is_not_an_unfixed_error(bridge, alias, corpus) -> None:
    case = _case(corpus, "neg-01")  # expected=tool, 结构保护跳过
    transport = RecordingTransport(
        default=_attempt("ok", 0.05, _ok_body("person", {"tool": 0.2, "person": 0.7, "unclear": 0.1}))
    )
    record = bm.run_case(
        transport, bridge, alias, case, source="main", retries=0, retry_delay_s=0.0, guard_replay="skip_negation"
    )
    metrics = bm.summarize_round([record], latency_note="t", is_repeat=False, budget_ms=350.0)
    tool = metrics["expected_tool"]
    assert tool["total_n"] == 1
    assert tool["ask_eligible_n"] == 0
    assert tool["guard_skipped_n"] == 1
    assert tool["guard_skipped_ids"] == ["neg-01"]
    assert metrics["budget"]["ask_n"] == 0
    assert metrics["rewrite_replay"]["would_replace_wrong_n"] == 0


def test_default_side_effects_do_not_touch_config_or_network(bridge, alias, corpus) -> None:
    """本文件的所有测试都不发网络请求；默认 retries=0，budget=350。"""
    args = bm.parse_args(["--cases", str(CASES_PATH), "--validate-only"])
    assert args.retries == 0
    assert args.budget_ms == 350.0
    assert args.timeout == 3.0
    assert args.compat_lexicon_heard is False


# ---------------------------------------------------------------- r4：负例分母与分桶互斥
STRONG_PERSON = {"tool": 0.05, "person": 0.9, "unclear": 0.05}
WEAK_PERSON = {"tool": 0.5, "person": 0.4, "unclear": 0.1}
STRONG_TOOL = {"tool": 0.9, "person": 0.05, "unclear": 0.05}


def _record(bridge, alias, corpus, case_id, *, probabilities, choice, guard_replay=None):
    case = _case(corpus, case_id)
    guard = guard_replay if guard_replay is not None else case["guard_expected"]
    transport = RecordingTransport(default=_attempt("ok", 0.05, _ok_body(choice, probabilities)))
    return bm.run_case(
        transport, bridge, alias, case, source="main", retries=0, retry_delay_s=0.0, guard_replay=guard
    )


def test_negatives_ask_denominator_counts_weak_once(bridge, alias, corpus) -> None:
    """weak 是“有效判断（分布合法）”的子集：ask 分母只按 guard_replay==ask 数记录。"""
    weak = _record(bridge, alias, corpus, "person-discuss-05", probabilities=WEAK_PERSON, choice="person")
    abstain = _record(bridge, alias, corpus, "person-help-05", probabilities=STRONG_PERSON, choice="person")
    mis_edit = _record(bridge, alias, corpus, "person-help-07", probabilities=STRONG_TOOL, choice="tool")
    bad = _record(bridge, alias, corpus, "person-discuss-04", probabilities=None, choice="tool")
    skipped = _record(
        bridge, alias, corpus, "person-help-01", probabilities=STRONG_PERSON, choice="person", guard_replay="skip_person"
    )
    tool = _record(bridge, alias, corpus, "tool-install-01", probabilities=STRONG_TOOL, choice="tool")
    assert (weak["gate_decision"], weak["dist_valid"]) == ("reject_weak", True)
    assert weak["replay_action"] == bm.REPLAY_KEPT_WEAK and weak["replay_correct"] is True
    assert (bad["replay_action"], bad["replay_correct"]) == (bm.REPLAY_KEPT_BAD_DIST, None)

    metrics = bm.summarize_round([weak, abstain, mis_edit, bad, skipped, tool], latency_note="t", is_repeat=False, budget_ms=350.0)
    ng = metrics["negatives_person_unclear"]
    et = metrics["expected_tool"]
    guard = metrics["structural_guard_replay"]
    assert (ng["n"], ng["ask_eligible_n"], ng["guard_skipped_n"], ng["unavailable_n"]) == (5, 4, 1, 0)
    assert (ng["effectively_judged_n"], ng["no_valid_verdict_n"]) == (3, 1)
    assert ng["ask_eligible_n"] == ng["effectively_judged_n"] + ng["no_valid_verdict_n"]
    # 旧缺陷口径（有效判断+无判定+弱拒绝）会把 weak 算两次，必须不等于新分母
    assert ng["ask_eligible_n"] != ng["effectively_judged_n"] + ng["no_valid_verdict_n"] + ng["judged_but_weak_rejected_n"]
    assert ng["judged_but_weak_rejected_n"] == 1
    assert ng["effective_wrong_replay_edit_n"] == 1
    assert ng["effective_wrong_replay_edit_ids"] == ["person-help-07"]
    assert ng["model_correct_abstain_n"] == 1 and ng["model_correct_abstain_ids"] == ["person-help-05"]
    # 安全保留 = 弱拒绝 + 合法拒绝 + 结构保护跳过；不等于模型识别正确
    assert ng["safe_keep_n"] == ng["kept_correct_n"] == 3
    assert ng["safe_keep_weak_rejected_n"] == 1
    assert ng["model_correct_abstain_n"] < ng["safe_keep_n"]
    assert guard["ask_n"] == et["ask_eligible_n"] + ng["ask_eligible_n"] == 5
    assert ng["n"] == ng["ask_eligible_n"] + ng["guard_skipped_n"] + ng["unavailable_n"]


def test_round_invariants_reject_double_counted_weak(bridge, alias, corpus) -> None:
    weak = _record(bridge, alias, corpus, "person-discuss-05", probabilities=WEAK_PERSON, choice="person")
    metrics = bm.summarize_round([weak], latency_note="t", is_repeat=False, budget_ms=350.0)
    broken = json.loads(json.dumps(metrics))
    ng = broken["negatives_person_unclear"]
    ng["ask_eligible_n"] = ng["effectively_judged_n"] + ng["no_valid_verdict_n"] + ng["judged_but_weak_rejected_n"]
    assert ng["ask_eligible_n"] == 2  # 旧口径：1 条弱拒绝被算成 2
    with pytest.raises(AssertionError, match="分桶不变量被破坏"):
        bm.assert_round_bucket_invariants([weak], broken)


def test_full_round_buckets_stay_exclusive_with_errors_and_bad_dists(bridge, alias, corpus) -> None:
    """整轮（99 条）混入超时/坏分布/弱分布：分桶仍互斥，bad/error 不算正确弃权。"""
    results = []
    guard_replays = []
    for index, case in enumerate(corpus["cases"]):
        if index % 11 == 0:
            results.append(_attempt("timeout", 3.0))
        elif index % 7 == 0:
            results.append(_attempt("ok", 0.05, _ok_body("person", None)))
        elif index % 3 == 0:
            results.append(_attempt("ok", 0.05, _ok_body("person", WEAK_PERSON)))
        else:
            results.append(_attempt("ok", 0.05, _ok_body("tool", STRONG_TOOL)))
        guard_replays.append(case["guard_expected"])
    transport = RecordingTransport(results=results)
    records = [
        bm.run_case(
            transport,
            bridge,
            alias,
            case,
            source="main",
            retries=0,
            retry_delay_s=0.0,
            guard_replay=guard_replays[index],
        )
        for index, case in enumerate(corpus["cases"])
    ]
    metrics = bm.summarize_round(records, latency_note="t", is_repeat=False, budget_ms=350.0)
    et = metrics["expected_tool"]
    ng = metrics["negatives_person_unclear"]
    guard = metrics["structural_guard_replay"]
    assert guard["ask_n"] == et["ask_eligible_n"] + ng["ask_eligible_n"]
    assert et["total_n"] == et["ask_eligible_n"] + et["guard_skipped_n"] + et["unavailable_n"]
    assert ng["n"] == ng["ask_eligible_n"] + ng["guard_skipped_n"] + ng["unavailable_n"]
    assert metrics["responses"]["final_error"] > 0 and metrics["responses"]["bad_distribution_n"] > 0
    bad_negatives = [
        r
        for r in records
        if r["expected"] != "tool" and r["guard_replay"] == "ask" and (r["final_status"] != "ok" or not r.get("dist_valid"))
    ]
    assert bad_negatives
    assert all(r["replay_correct"] is None for r in bad_negatives)
    assert ng["safe_keep_n"] == sum(
        1
        for r in records
        if r["expected"] != "tool" and r.get("replay_correct") is True and r["guard_replay"] != bm.GUARD_UNAVAILABLE
    )
    assert ng["model_correct_abstain_n"] <= ng["safe_keep_n"]


# ------------------------------------------------- r5：弱分布不算无有效判定 / 门槛重放口径
# 与审查报告 D1 的真实样例同形：top 概率低于 0.70，属合法弱分布拒绝。
WEAK_TOOL = {"tool": 0.6086, "person": 0.3007, "unclear": 0.0907}


def _error_record(bridge, alias, corpus, case_id, status="timeout"):
    transport = RecordingTransport(default=_attempt(status, 3.0))
    return bm.run_case(
        transport,
        bridge,
        alias,
        _case(corpus, case_id),
        source="main",
        retries=0,
        retry_delay_s=0.0,
        guard_replay="ask",
    )


def test_tool_weak_rejection_is_not_no_valid_verdict(bridge, alias, corpus) -> None:
    """D1：分布合法的弱分布拒绝（gate_result=None）不是“无有效判定”，单列 weak_rejected。

    历史失真：ask_eligible_no_valid_verdict_n 把 gate_result=None 也算进去，
    SemIf cold 轮 12 条合法弱拒绝被误报成 12 条无有效判定。
    """
    weak = _record(bridge, alias, corpus, "tool-install-01", probabilities=WEAK_TOOL, choice="tool")
    bad = _record(bridge, alias, corpus, "tool-config-01", probabilities=None, choice="tool")
    error = _error_record(bridge, alias, corpus, "tool-start-01")
    assert weak["guard_replay"] == "ask" and weak["dist_valid"] is True
    assert (weak["gate_decision"], weak["gate_result"]) == ("reject_weak", None)
    assert (bad["gate_decision"], bad["dist_valid"]) == ("unusable_bad_distribution", False)

    metrics = bm.summarize_round([weak, bad, error], latency_note="t", is_repeat=False, budget_ms=350.0)
    et = metrics["expected_tool"]
    assert et["ask_eligible_n"] == 3
    assert et["ask_eligible_accepted_n"] == 0
    assert et["ask_eligible_weak_rejected_n"] == 1
    assert et["ask_eligible_weak_rejected_ids"] == ["tool-install-01"]
    # 只有错误/坏分布算无有效判定；旧口径会给出 3（把弱拒绝一起算）
    assert et["ask_eligible_no_valid_verdict_n"] == 2
    assert et["ask_eligible_no_valid_verdict_ids"] == ["tool-config-01", "tool-start-01"]
    assert et["ask_eligible_rejected_non_tool_n"] == 0
    assert (
        et["ask_eligible_accepted_n"]
        + et["ask_eligible_weak_rejected_n"]
        + et["ask_eligible_rejected_non_tool_n"]
        + et["ask_eligible_no_valid_verdict_n"]
        == et["ask_eligible_n"]
    )

    broken = json.loads(json.dumps(metrics))
    broken["expected_tool"]["ask_eligible_no_valid_verdict_n"] = 3  # 旧口径
    broken["expected_tool"]["ask_eligible_weak_rejected_n"] = 0
    with pytest.raises(AssertionError, match="弱拒绝/合法判定被算进来"):
        bm.assert_round_bucket_invariants([weak, bad, error], broken)


def test_gate_replay_scope_separates_global_counterfactual_from_ask_subset(bridge, alias, corpus) -> None:
    """D2：全量反事实含结构保护跳过的请求，必须标注 scope 且与 ask 子集计数分开。

    历史失真：gate_replay 的弱拒绝数按全部 99 条 HTTP 记录计（cold 轮 46），
    note 却说“结构保护之后”，容易被当成 ask 子集（22）。
    """
    skipped_weak = _record(
        bridge, alias, corpus, "person-help-01", probabilities=WEAK_PERSON, choice="person", guard_replay="skip_person"
    )
    asked_weak = _record(bridge, alias, corpus, "tool-install-01", probabilities=WEAK_TOOL, choice="tool")
    assert (skipped_weak["guard_replay"], skipped_weak["gate_decision"]) == ("skip_person", "reject_weak")
    assert asked_weak["guard_replay"] == "ask"

    metrics = bm.summarize_round([skipped_weak, asked_weak], latency_note="t", is_repeat=False, budget_ms=350.0)
    g = metrics["gate_replay_0_70_0_20"]
    assert g["scope"] == "all_requests_counterfactual"
    assert "结构保护" in g["scope_note"]
    # 全量反事实包含 guard 跳过的反事实请求
    assert g["reject_weak"] == 2
    # ask 子集只算产品真正会发出的那条
    assert g["ask_subset_n"] == 1
    assert g["ask_subset_reject_weak_n"] == 1
    assert g["reject_weak"] != g["ask_subset_reject_weak_n"]
    assert (
        g["ask_subset_accept_tool_n"]
        + g["ask_subset_accept_person_n"]
        + g["ask_subset_accept_unclear_n"]
        + g["ask_subset_reject_weak_n"]
        + g["ask_subset_unusable_bad_distribution_n"]
        + g["ask_subset_no_answer_error_n"]
        == g["ask_subset_n"]
    )

    # 把全局反事实数当 ask 子集数（或反过来）必须被不变量拦住
    broken = json.loads(json.dumps(metrics))
    broken["gate_replay_0_70_0_20"]["ask_subset_reject_weak_n"] = 2
    with pytest.raises(AssertionError, match="ask 子集分桶不互斥"):
        bm.assert_round_bucket_invariants([skipped_weak, asked_weak], broken)
    unscoped = json.loads(json.dumps(metrics))
    unscoped["gate_replay_0_70_0_20"]["scope"] = "product_path"
    with pytest.raises(AssertionError, match="scope 未标明全量反事实"):
        bm.assert_round_bucket_invariants([skipped_weak, asked_weak], unscoped)
