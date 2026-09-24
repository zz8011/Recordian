"""Start-up readiness wrapper for the decider trial on 111 (Recordian).

Why
---
The official server answers ``GET /health`` as soon as the engine is loaded and sealed, but
the first real forward still pays the whole initialisation (weight page-in + kernel JIT).
The trial measured 53089.933 ms for the first Chinese role choice against ~120 ms for every
later one, so a user whose first sentence arrives right after ``/health`` turns 200 times out
even though health said the server was ready.

What
----
This module imports the official ``decider.serve`` **unchanged** and only extends its
lifespan: after the official start-up (engine loaded, sealed, batcher alive) and *before* the
lifespan yields, two Recordian-shaped Chinese role choices run through the official request
path (``decider.serve.systemone`` -- the same queue, batcher and GPU thread a real request
uses).  The lifespan yields once both answers are protocol-legal choice distributions, so the
port becomes reachable exactly when a real request is already fast.  A warmup that fails
writes the report and re-raises, so uvicorn logs ``Application startup failed. Exiting.``
and never binds the port instead of advertising a health the first sentence cannot use.

Legality is about the protocol and the numbers, never about confidence or correctness: a
uniform distribution and a high-confidence distribution are both legal, and the warmup does
not care whether the model picked the "right" label (the cases are not graded).  Readiness
means "the engine just answered two real requests with well-formed choices", nothing more.

Nothing official is modified here: no scoring, no temperature, no weights, no layout, no
graph/eager policy, no torch.compile, no FP8.  The wrapper only *asserts* the trial policy it
was started with and refuses to run if it drifted.

Run it instead of the official module::

    cd /work/src/decider
    python -m uvicorn serve_warmup:app --app-dir /work/warmup --host 0.0.0.0 --port 42071

Env: ``DECIDER_WARMUP_REPORT`` (default /work/logs/warmup-report.json),
``DECIDER_WARMUP_TIMEOUT_S`` (per case, default 300).
"""

import asyncio
import json
import math
import os
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import decider.serve as serve

REPORT_PATH = os.environ.get("DECIDER_WARMUP_REPORT", "/work/logs/warmup-report.json")
CASE_TIMEOUT_S = float(os.environ.get("DECIDER_WARMUP_TIMEOUT_S", "300"))

# Legality of one answer, with no confidence threshold anywhere: labels must match the request,
# every probability must be a finite number in [0, 1], the mass must be ~1, and ``choice`` must
# be one of the labels and agree with the largest probability.  A uniform or a saturated-but-
# legal distribution is fine -- the wrapper absorbs JIT, it does not grade the model.
MASS_TOL = 0.02
ARGMAX_EPS = 1e-9

# Exactly the Recordian role question: the same instructions and criteria the product sends.
INSTR = (
    "判断句子中标记的「{surface}」在这个语境里指的是什么。"
    "只能从给定候选里选一个。拿不准就选“不清楚”。"
    "补充说明：用户的常用词里，jev 是这个用户的软件工具；语音识别经常把它误写成 {heard}。"
)
CRITERIA = {
    "tool": "这里指的是软件工具、程序或插件。",
    "person": "这里指的是一个人。",
    "unclear": "不清楚，或者以上都不是。",
}

# At least two cases, one of them with a long state (the smoke states were ~120 tokens; the
# long one here is ~4x that).  They are the real product-shaped role choices, so a real forward
# has to run; their answers are not graded and any well-formed distribution is accepted.  They
# are not reused by the post-ready acceptance requests.
CASES = [
    {
        "id": "warmup-script-tool",
        "surface": "jeff",
        "heard": "jeff",
        "context": "",
        "focus": "把jeff升到最新版之后，之前那些自动化脚本就报错了",
    },
    {
        "id": "warmup-long-person",
        "surface": "Jeff",
        "heard": "Jeff",
        "context": "",
        "focus": (
            "上周三的评审会上，Jeff把下一季度的排期逐条讲了一遍，从设计定稿到上线窗口都给了明确的时间点。"
            "他说测试环境要等到下周一才能空出来，所以联调得往后挪两天，还建议灰度比例先从百分之五开始，"
            "观察一整晚再决定要不要放大到百分之三十。会后他把纪要发到了群里，又单独提醒我补一份容量评估，"
            "强调这次的重点是先把风险讲清楚，不要急着承诺时间，宁可多花两天把边界情况想周全。\n"
            "今天早上我又跟他确认了一遍，他说只要设计稿周四能定稿，周五就可以安排第一轮回归，"
            "周末他本人会盯着监控，有问题随时给他打电话。中午他还拉着我和另外两位同事过了一遍回滚预案，"
            "把每一步的责任人都写清楚了，连通知顺序和对外口径都排好了。下午他请了半天假去接孩子，"
            "临走前把待办清单发给大家，说晚上有空会再看一眼，让我们遇到拿不准的地方先记下来，明天一早一起讨论。\n"
            "我对他的印象一直是稳，虽然说话慢一点，但每次都能把最坏的情况先摆出来，"
            "所以大家遇到跨部门的事情都愿意先听听他的意见。他刚来的时候还不太熟悉我们的流程，"
            "现在已经能独立带一整条线了，上个月的复盘还是他主动牵头做的，连会议纪要都写得比别人清楚。"
        ),
    },
]


def _utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def payload_for(case):
    """The wire payload of one warmup case, in the same shape the trial's real requests use."""
    lines = []
    if case.get("context"):
        lines.append("上一段：" + case["context"])
    lines.append(case["focus"])
    lines.append("只判断这个跨度：「{}」".format(case["surface"]))
    return {
        "state": "\n".join(lines),
        "questions": {
            "role": {
                "type": "choice",
                "instructions": INSTR.format(surface=case["surface"], heard=case["heard"]),
                "criteria": dict(case.get("criteria") or CRITERIA),
            }
        },
    }


def check_answer(case, resp):
    """(ok, problems, summary) for one raw /v1/systemone answer.

    Mechanical, confidence-free legality: the response must be a choice over exactly the
    requested labels, every probability must be a finite number in [0, 1], the mass must be
    1 +/- MASS_TOL, and ``choice`` must be one of the labels and agree with the largest
    probability (ties are legal).  Whether the distribution is flat or peaky, and whether the
    chosen label is the one a human would pick, is deliberately not part of readiness."""
    ans = (resp.get("answers") or {}).get("role") or {}
    probs = ans.get("probabilities") or {}
    problems = []
    if ans.get("type") != "choice":
        problems.append("answer type is {!r}, not 'choice'".format(ans.get("type")))
    wanted = set(case.get("criteria") or CRITERIA)
    if set(probs) != wanted:
        problems.append(f"labels {sorted(probs)!r} != requested {sorted(wanted)!r}")
    vals = {}
    for key, value in probs.items():
        try:
            vals[key] = float(value)
        except (TypeError, ValueError):
            problems.append(f"probability {value!r} for {key!r} is not a number")
    if not vals:
        problems.append("no probabilities in the answer")
    else:
        non_finite = sorted(k for k, v in vals.items() if not math.isfinite(v))
        if non_finite:
            problems.append(f"probability is not finite for {non_finite!r}")
        outside = sorted(k for k, v in vals.items() if math.isfinite(v) and not 0.0 <= v <= 1.0)
        if outside:
            problems.append(f"probability outside [0, 1] for {outside!r}")
        finite = [v for v in vals.values() if math.isfinite(v)]
        top = max(finite) if finite else None
        total = math.fsum(finite)
        if abs(total - 1.0) > MASS_TOL:
            problems.append(f"probability mass {total:.6f} is not normalised")
        choice = ans.get("choice")
        if choice not in vals:
            problems.append(f"choice {choice!r} is not one of the labels")
        elif top is not None and vals[choice] < top - ARGMAX_EPS:
            problems.append(f"choice {choice!r} ({vals[choice]:.6f}) is not the largest probability ({top:.6f})")
    summary = {
        "choice": ans.get("choice"),
        "probabilities": probs,
        "confidence": ans.get("confidence"),
        "top": max((v for v in vals.values() if math.isfinite(v)), default=None),
    }
    return (not problems), problems, summary


def policy_snapshot():
    eng = getattr(serve, "eng", None)
    return {
        "temperature": getattr(serve, "TEMP", None),
        "layout": getattr(serve, "LAYOUT", None),
        "schema_first": getattr(serve, "SCHEMA_FIRST", None),
        "decider_warmup_env": getattr(serve, "WARMUP", None),
        "compile": getattr(serve, "COMPILE", None),
        "fp8": getattr(serve, "FP8", None),
        "model": getattr(serve, "MODEL", None),
        "model_name": getattr(serve, "MODEL_NAME", None),
        "shared": getattr(serve, "SHARED", None),
        "max_state_tokens": getattr(serve, "MAX_STATE_TOKENS", None),
        "engine_sealed": bool(getattr(eng, "sealed", False)),
        "graph_captures": len(getattr(eng, "graphs", {}) or {}),
        "engine_cfg": {k: v for k, v in (getattr(eng, "cfg", {}) or {}).items() if k not in ("t_buckets", "b_buckets")},
    }


def check_official_policy():
    """Fail start-up when the trial policy drifted, instead of warming up a different server."""
    problems = []
    expected = (
        ("TEMP", 1.935),
        ("LAYOUT", "plain"),
        ("SCHEMA_FIRST", False),
        ("WARMUP", False),
        ("COMPILE", False),
        ("FP8", False),
        ("SHARED", False),
    )
    for name, want in expected:
        got = getattr(serve, name, "<missing>")
        same = abs(float(got) - float(want)) <= 1e-9 if isinstance(want, float) else got == want
        if not same:
            problems.append(f"{name}={got!r}, expected {want!r}")
    eng = getattr(serve, "eng", None)
    if eng is None:
        problems.append("engine is None")
    else:
        if not getattr(eng, "sealed", False):
            problems.append("engine is not sealed before warmup")
        if len(getattr(eng, "graphs", {}) or {}):
            problems.append(f"engine captured {len(eng.graphs)} graphs (DECIDER_WARMUP=0 must stay eager)")
    if problems:
        raise RuntimeError("warmup refuses to run, official trial policy drifted: " + "; ".join(problems))


def _write_report(report):
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        tmp = REPORT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, REPORT_PATH)
    except Exception:
        traceback.print_exc()


async def run_warmup(cases=None):
    """Run the cases through the official route handler and refuse to continue on a failure."""
    cases = CASES if cases is None else cases
    t_begin = time.perf_counter()
    report = {
        "status": "running",
        "started_utc": _utc(),
        "report_path": REPORT_PATH,
        "wrapper": "candidate/server/decider_trial/scripts/serve_warmup.py",
        "policy": policy_snapshot(),
        "legality": (
            "choice protocol; labels == requested criteria; every probability finite and in [0, 1]; "
            f"mass 1 +/- {MASS_TOL:.2f}; choice in labels and equal to the largest probability (ties allowed). "
            "No confidence or accuracy gate."
        ),
        "mass_tol": MASS_TOL,
        "cases": [],
    }
    print("[warmup] begin cases={} report={}".format([c["id"] for c in cases], REPORT_PATH), flush=True)
    for case in cases:
        payload = payload_for(case)
        row = {"id": case["id"], "state_chars": len(payload["state"]), "state": payload["state"], "payload": payload}
        t0 = time.perf_counter()
        summary, problems = {}, []
        try:
            resp = await asyncio.wait_for(serve.systemone(serve.S1Req(**payload)), timeout=CASE_TIMEOUT_S)
            row["ms"] = round((time.perf_counter() - t0) * 1000, 3)
            row["response_raw"] = resp
            ok, problems, summary = check_answer(case, resp)
        except Exception as exc:
            row["ms"] = round((time.perf_counter() - t0) * 1000, 3)
            row["error"] = f"{type(exc).__name__}: {exc}"
            ok = False
            problems = [row["error"]]
        row.update(summary)
        row["ok"] = ok
        row["problems"] = problems
        report["cases"].append(row)
        print(
            "[warmup] {} ok={} ms={:.1f} choice={} probs={} problems={}".format(
                case["id"],
                ok,
                row["ms"],
                summary.get("choice"),
                json.dumps(summary.get("probabilities"), ensure_ascii=False),
                problems,
            ),
            flush=True,
        )
        if not ok:
            report["status"] = "failed"
            report["failed_case"] = case["id"]
            report["finished_utc"] = _utc()
            report["total_ms"] = round((time.perf_counter() - t_begin) * 1000, 3)
            _write_report(report)
            raise RuntimeError("warmup case {} failed: {}".format(case["id"], "; ".join(problems)))
    report["status"] = "ok"
    report["finished_utc"] = _utc()
    report["total_ms"] = round((time.perf_counter() - t_begin) * 1000, 3)
    report["policy_after"] = policy_snapshot()
    _write_report(report)
    print(f"[warmup] READY cases={len(report['cases'])} total_ms={report['total_ms']:.1f}", flush=True)
    return report


_original_lifespan = serve.app.router.lifespan_context


@asynccontextmanager
async def warmup_lifespan(app):
    """Official lifespan, then warmup, then (and only then) readiness.  The official lifespan
    state is forwarded unchanged, so Starlette/FastAPI see exactly what they saw before."""
    async with _original_lifespan(app) as state:
        check_official_policy()
        print("[warmup] engine loaded and sealed, warming before readiness", flush=True)
        await run_warmup()
        yield state


serve.app.router.lifespan_context = warmup_lifespan
app = serve.app
