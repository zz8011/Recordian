#!/bin/bash
# Eight new SystemOne choice sentences against the running trial. Not the 99-case benchmark.
set -u
ROOT=/media/v/Data/recordian-decider-trial
OUT=$ROOT/logs/smoke-choices.json
date -u +%Y-%m-%dT%H:%M:%SZ
python3 - << 'PY'
import json, math, time, urllib.request
URL = "http://192.168.5.111:42071/v1/systemone"
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
CASES = [
    {"id": "tool-install-deps", "surface": "jeff", "heard": "jeff", "context": "", "focus": "先用jeff把这个仓库的依赖装好"},
    {"id": "person-budget-mail", "surface": "Jeff", "heard": "Jeff", "context": "", "focus": "Jeff昨天发邮件说预算批下来了"},
    {"id": "tool-hung-restart", "surface": "jeff", "heard": "jeff", "context": "", "focus": "刚才jeff卡住了，帮我重启一下服务"},
    {"id": "person-wrote-plugin", "surface": "Jeff", "heard": "Jeff", "context": "", "focus": "那个插件是Jeff写的，问他比较快"},
    {"id": "unclear-ask-later", "surface": "jeff", "heard": "jeff", "context": "", "focus": "这个功能先问下jeff再说"},
    {"id": "negated-run-leak", "surface": "jeff", "heard": "jeff", "context": "", "focus": "别运行jeff，它有内存泄漏"},
    {"id": "context-tool-then-person", "surface": "Jeff", "heard": "Jeff", "context": "刚才已经打开了jeff工具", "focus": "Jeff说明天再看日志"},
    {"id": "mixed-restart-then-read", "surface": "jeff", "heard": "jeff", "context": "", "focus": "请把jeff重启一下，然后让Jeff看一下日志"},
]

def payload(case):
    lines = []
    if case["context"]:
        lines.append("上一段：" + case["context"])
    lines.append(case["focus"])
    lines.append("只判断这个跨度：「%s」" % case["surface"])
    return {
        "state": "\n".join(lines),
        "questions": {
            "role": {
                "type": "choice",
                "instructions": INSTR.format(surface=case["surface"], heard=case["heard"]),
                "criteria": CRITERIA,
            }
        },
    }

def once(case):
    body = json.dumps(payload(case), ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            status = resp.status
    except Exception as exc:
        return {"id": case["id"], "ok": False, "error": type(exc).__name__, "detail": str(exc)[:300], "ms": round((time.perf_counter() - t0) * 1000, 3)}
    ms = (time.perf_counter() - t0) * 1000
    parsed = json.loads(raw)
    ans = (parsed.get("answers") or {}).get("role") or {}
    return {
        "id": case["id"],
        "ok": status == 200 and ans.get("type") == "choice",
        "http": status,
        "ms": round(ms, 3),
        "model": parsed.get("model"),
        "choice": ans.get("choice"),
        "probabilities": ans.get("probabilities"),
        "confidence": ans.get("confidence"),
        "usage": parsed.get("usage"),
        "focus": case["focus"],
    }

def pct(xs, q):
    if not xs:
        return None
    ordered = sorted(xs)
    idx = (len(ordered) - 1) * q
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return round(ordered[lo], 3)
    w = idx - lo
    return round(ordered[lo] * (1 - w) + ordered[hi] * w, 3)

# health
t0 = time.perf_counter()
health = {}
for _ in range(90):
    try:
        with urllib.request.urlopen("http://192.168.5.111:42071/health", timeout=5) as resp:
            health = {"http": resp.status, "body": resp.read().decode()[:500], "wait_s": round(time.perf_counter() - t0, 3)}
            break
    except Exception as exc:
        health = {"error": type(exc).__name__, "detail": str(exc)[:200]}
        time.sleep(2)
else:
    json.dump({"health": health}, open("/media/v/Data/recordian-decider-trial/logs/smoke-choices.json", "w"), ensure_ascii=False, indent=2)
    raise SystemExit("health timeout")

first = once(CASES[0])
rest_first = [once(c) for c in CASES[1:]]
pass_a = [first] + rest_first
pass_b = [once(c) for c in CASES]
ok_b = [r["ms"] for r in pass_b if r.get("ok")]

# Bad/degenerate distribution flags: no answer, non-normalised mass, one label
# swallowing everything, or a label outside the requested criteria set.
CRITERIA_KEYS = set(CRITERIA)
bad = []
for r in pass_b:
    probs = r.get("probabilities") or {}
    if not r.get("ok"):
        bad.append({"id": r["id"], "why": "no_choice", "detail": r.get("error") or r.get("http")})
        continue
    if set(probs) != CRITERIA_KEYS:
        bad.append({"id": r["id"], "why": "label_mismatch", "labels": sorted(probs)})
    total = sum(probs.values())
    if abs(total - 1.0) > 0.02:
        bad.append({"id": r["id"], "why": "mass_not_normalised", "total": round(total, 6)})
    # Historical diagnostic from the deployment smoke run: these confidence bands flag "worth a
    # look" in that report only.  They are NOT a readiness or protocol criterion -- the warmup
    # gate (warmup/serve_warmup.py) accepts any well-formed distribution, flat or saturated.
    top = max(probs.values()) if probs else 0.0
    if top > 0.999:
        bad.append({"id": r["id"], "why": "saturated_single_label", "top": round(top, 6), "choice": r.get("choice")})
    if top < 0.40:
        bad.append({"id": r["id"], "why": "flat_distribution", "top": round(top, 6), "choice": r.get("choice")})

report = {
    "health": health,
    "first_request_includes_compile": first,
    "pass_a_coldish": pass_a,
    "pass_b_warm": pass_b,
    "warm_ms": {"n": len(ok_b), "p50": pct(ok_b, 0.50), "p95": pct(ok_b, 0.95), "max": round(max(ok_b), 3) if ok_b else None},
    "cold_ms": first.get("ms"),
    "distribution_checks": {
        "criteria_labels": sorted(CRITERIA_KEYS),
        "bad_count": len(bad),
        "bad": bad,
        "choices": {r["id"]: r.get("choice") for r in pass_b},
    },
}
json.dump(report, open("/media/v/Data/recordian-decider-trial/logs/smoke-choices.json", "w"), ensure_ascii=False, indent=2)
print("SMOKE_WROTE", report["warm_ms"])
print("FIRST", first.get("id"), first.get("ms"), first.get("choice"), first.get("ok"))
PY
echo SMOKE_EXIT $?
