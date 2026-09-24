#!/usr/bin/env python3
"""从既有 benchmark 报告的原始 records 离线重算指标（**不发任何 HTTP 请求**）。

用途：修正统计口径（例如负例 ask 分母把 weak 子集重复相加）后，用同一批 records、
同一批原始 payload 重算报告，并把来源写清楚：

* ``source_report_path`` / ``source_report_sha256``：被重算的旧报告；
* ``original_tool_sha256``：产生旧报告的 ``benchmark.py`` 哈希（取自旧报告）；
* ``recomputed_by_tool`` / ``recomputed_by_tool_sha256``：本次重算用的工具与哈希；
* ``recomputed_at`` / ``http_rerun=false``：没有重新发请求；HTTP 时间
  （``duration_ms`` / ``engine_ms`` / ``attempts``）与原始 payload 逐条保留；
* 逐条核对 ``request_payload_sha256``、corpus sha256 与整批 records 的规范哈希未变；
* ``metric_diff_vs_source``：新旧指标差异路径，证明只改了该改的字段。

用法：
python3 recompute_report.py --source ../reports/x.json \\
    --output ../reports/x-v3.json --summary-md ../reports/x-v3.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark as bm  # noqa: E402

# 旧报告里与“必须同温度”相关的过时 caveat 标记；重算时用新口径替换。
STALE_COMPARABILITY_MARKERS = (
    "禁止宣称 gate 跨模型",
    "跨模型 gate 比较还需",
    "只能比较原始 argmax",
    "有效温度已核实；跨模型",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metric_diff(old: Any, new: Any, prefix: str = "") -> list[dict[str, Any]]:
    """列出新旧 metrics 的差异路径（末级值不同才算差异）。"""
    diffs: list[dict[str, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            diffs.extend(metric_diff(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else str(key)))
    elif old != new:
        diffs.append({"path": prefix, "old": old, "new": new})
    return diffs


def refresh_caveats(caveats: list[str], comparability: dict[str, Any]) -> list[str]:
    """保留非温度类 caveat；温度/可比性口径换成当前工具的统一文案。"""
    kept = [c for c in caveats if not any(marker in c for marker in STALE_COMPARABILITY_MARKERS)]
    kept.append(
        "顶层 temperature/model 不发送（产品 request_choices 也不发送）；部署配置与发布温度逐模型核实，"
        "未核实写 unknown 并显著标注（该 run 不满足比较前置条件）。"
    )
    kept.extend(bm.comparability_caveats(comparability))
    return kept


def provenance_markdown(provenance: dict[str, Any]) -> str:
    diffs = provenance["metric_diff_vs_source"]
    denominator_diffs = [d for d in diffs if d["path"].endswith("negatives_person_unclear.ask_eligible_n")]
    key_markers = (
        "expected_tool.ask_eligible_no_valid_verdict_n",
        "expected_tool.ask_eligible_weak_rejected_n",
        "expected_tool.ask_eligible_rejected_non_tool_n",
        "gate_replay_0_70_0_20.scope",
        "gate_replay_0_70_0_20.ask_subset_",
        "gate_replay_0_70_0_20.scope_note",
        "gate_replay_0_70_0_20.ask_subset_note",
    )
    key_diffs = [d for d in diffs if d not in denominator_diffs and any(m in d["path"] for m in key_markers)]
    other_diffs = [d for d in diffs if d not in denominator_diffs and d not in key_diffs]
    other_fields = sorted({str(d["path"]) for d in other_diffs})
    if denominator_diffs:
        denominator_line = "- 关键差异（负例分母，其他分桶不再相加）：" + "；".join(
            f"r{d['round_index']} {d['old']}→{d['new']}" for d in denominator_diffs
        )
    else:
        denominator_line = "- 关键差异（负例分母）：无"
    key_line = (
        "- 关键差异（R5 口径修正）："
        + "；".join(f"r{d['round_index']} `{d['path']}` {d['old']!r}→{d['new']!r}" for d in key_diffs)
        if key_diffs
        else "- 关键差异（R5 口径修正）：无"
    )
    lines = [
        "",
        "## 重算来源（离线，无新 HTTP）",
        "",
        f"- 来源报告：`{provenance['source_report_path']}` sha256=`{provenance['source_report_sha256']}`"
        f"（原生成时间 {provenance['source_generated_at']}）",
        f"- 原工具 sha256=`{provenance['original_tool_sha256']}`（报告头部 `versions.tool_sha256` 保留此原值）；"
        f"本次重算工具 `{provenance['recomputed_by_tool']}` sha256=`{provenance['recomputed_by_tool_sha256']}`；"
        f"驱动脚本 `{provenance['recompute_driver']}` sha256=`{provenance['recompute_driver_sha256']}`",
        f"- 重算时间：{provenance['recomputed_at']}；http_rerun=`{provenance['http_rerun']}`"
        "（只读既有 records，未发任何请求；HTTP 时间与原始 payload 逐条保留）",
        f"- 语料 sha256=`{provenance['corpus_sha256']}`（未变化=`{provenance['corpus_sha256_unchanged']}`）；"
        f"逐条 payload sha256 核对 {provenance['records_payload_sha256_checked_n']} 条，"
        f"不一致 {len(provenance['records_payload_sha256_mismatch_ids'])} 条；"
        f"整批 records 规范哈希=`{provenance['records_canonical_sha256']}`（逐条原样保留）",
        denominator_line,
        key_line,
        f"- 取代说明：{provenance['supersedes_note']}",
        f"- 其余差异 {len(other_diffs)} 处（新增字段 / note 文案）："
        + ("、".join(f"`{field}`" for field in other_fields) or "无")
        + "；完整明细见 JSON `recompute_provenance.metric_diff_vs_source`",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从既有报告的 records 离线重算指标（不发请求）")
    parser.add_argument("--source", required=True, help="既有 JSON 报告（含原始 records）")
    parser.add_argument("--output", required=True, help="重算后的 JSON 输出路径")
    parser.add_argument("--summary-md", default="", help="可选：Markdown 摘要输出路径")
    parser.add_argument("--label", default="", help="报告标签（默认沿用来源标签 + '+recomputed'）")
    parser.add_argument("--notes", default="", help="自由备注（默认沿用来源 notes）")
    args = parser.parse_args(argv)

    source_path = Path(args.source).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source_report_sha256 = sha256_file(source_path)
    tool_path = Path(bm.__file__).resolve()
    budget_ms = float(source["run"]["budget_ms"])

    rounds: list[dict[str, Any]] = []
    metric_diffs: list[dict[str, Any]] = []
    payload_mismatch_ids: list[str] = []
    payload_checked = 0
    for item in source["rounds"]:
        records = item["records"]
        for record in records:
            declared = record.get("request_payload_sha256")
            payload = record.get("request_payload")
            if declared is None or payload is None:
                continue
            payload_checked += 1
            if bm.canonical_sha256(payload) != declared:
                payload_mismatch_ids.append(str(record["case_id"]))
        old_metrics = item["metrics"]
        metrics = bm.summarize_round(
            records,
            latency_note=str(old_metrics.get("latency_note", "")),
            is_repeat=bool(old_metrics.get("is_repeat_of_identical_sentences")),
            budget_ms=budget_ms,
        )
        metric_diffs.extend(
            {"round_index": item["round_index"], **diff} for diff in metric_diff(old_metrics, metrics)
        )
        rounds.append(
            {
                "round_index": item["round_index"],
                "round_type": item["round_type"],
                "description": item["description"],
                "n_cases": len(records),
                "metrics": metrics,
                "records": records,
            }
        )

    if payload_mismatch_ids:
        print(f"payload sha256 核对失败：{payload_mismatch_ids}", file=sys.stderr)
        return 2

    source_cases_path = Path(str(source["versions"].get("cases_path") or ""))
    corpus_sha256 = None
    corpus_unchanged: bool | None = None
    if source_cases_path.is_file():
        corpus_sha256 = sha256_file(source_cases_path)
        corpus_unchanged = corpus_sha256 == source["versions"].get("cases_sha256")

    def _records_digest(round_items: list[dict[str, Any]]) -> str:
        return bm.canonical_sha256([record for item in round_items for record in item["records"]])

    source_records_sha256 = _records_digest(source["rounds"])
    output_records_sha256 = _records_digest(rounds)
    if source_records_sha256 != output_records_sha256:
        print("records 未能逐条保留（规范哈希不一致）", file=sys.stderr)
        return 2

    temperature = source["temperature"]
    comparability = bm.comparability_block(temperature)
    report = dict(source)
    report["label"] = args.label or f"{source.get('label')}+recomputed"
    report["notes"] = args.notes or source.get("notes", "")
    report["comparability"] = comparability
    report["caveats"] = refresh_caveats(list(source.get("caveats") or []), comparability)
    report["rounds"] = rounds
    report["recompute_provenance"] = {
        "source_report_path": str(source_path),
        "source_report_sha256": source_report_sha256,
        "source_generated_at": source.get("generated_at"),
        "original_tool_sha256": source["versions"].get("tool_sha256"),
        "recomputed_by_tool": str(tool_path),
        "recomputed_by_tool_sha256": sha256_file(tool_path),
        "recompute_driver": str(Path(__file__).resolve()),
        "recompute_driver_sha256": sha256_file(Path(__file__).resolve()),
        "recomputed_at": bm.now_iso(),
        "http_rerun": False,
        "http_rerun_note": (
            "只读既有 records 离线重算，没有重新发任何 HTTP 请求；duration_ms / engine_ms / attempts "
            "与完整 request_payload 逐条原样保留（records 规范哈希一致）。"
        ),
        "supersedes_note": (
            "本报告取代来源报告的统计口径与温度/可比性 caveat 文案（来源报告原文保留不改）："
            "来源若把负例 ask 分母写成 有效判断+无判定+弱拒绝，则 weak 子集被重复相加；"
            "来源若给各 run 的发布温度设了统一要求，也与“按各自发布配置对照”矛盾。"
            "R5 再修两处口径：expected_tool.ask_eligible_no_valid_verdict_n 只计 response 错误/坏分布，"
            "分布合法的弱分布拒绝改记 ask_eligible_weak_rejected_n（不是无有效判定）；"
            "gate_replay_0_70_0_20 顶层 accept_*/reject_weak 明确 scope=all_requests_counterfactual"
            "（含结构保护本会跳过的反事实请求，不等于产品路径），产品路径用 ask_subset_* 与 rewrite_replay，"
            "两套口径不可相加。两处均以本报告为准。"
        ),
        "corpus_sha256": corpus_sha256 if corpus_sha256 is not None else source["versions"].get("cases_sha256"),
        "corpus_sha256_unchanged": corpus_unchanged,
        "corpus_sha256_note": (
            "用当前 cases.json 重新计算并与来源报告记录比对；文件不存在时为 null。"
            if corpus_unchanged is not None
            else "语料文件当前不可读，沿用来源报告记录的 sha256（未重新验证文件）。"
        ),
        "records_payload_sha256_checked_n": payload_checked,
        "records_payload_sha256_mismatch_ids": payload_mismatch_ids,
        "records_canonical_sha256": source_records_sha256,
        "records_preserved_verbatim": True,
        "metric_diff_vs_source": metric_diffs,
        "rounds_recomputed": len(rounds),
        "records_n": sum(len(item["records"]) for item in rounds),
    }
    report["summary_markdown"] = bm.render_summary_markdown(report) + provenance_markdown(
        report["recompute_provenance"]
    )

    output_path = Path(args.output)
    if output_path.parent and not output_path.parent.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.summary_md:
        md_path = Path(args.summary_md)
        if md_path.parent and not md_path.parent.exists():
            md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(report["summary_markdown"], encoding="utf-8")
    print(f"指标差异 {len(metric_diffs)} 处：")
    for diff in metric_diffs:
        print(f"  r{diff['round_index']} {diff['path']}: {diff['old']!r} -> {diff['new']!r}")
    print(f"重算报告已写入 {output_path}" + (f" 和 {args.summary_md}" if args.summary_md else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
