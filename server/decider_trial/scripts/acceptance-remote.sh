#!/bin/bash
# Post-readiness acceptance for the warmup change:
#   * two *new* Chinese role choices (never used as warmup payloads), each under 350 ms,
#   * legality of every distribution, with no confidence gate: choice protocol, labels, every
#     probability finite and inside [0, 1], mass ~1, choice in labels and equal to the argmax
#     (the human-expected label is recorded but never used as a pass/fail criterion),
#   * container health (OOMKilled, restarts, memory, port binding, exit code),
#   * the warmup report that gated readiness, and SemIf 42032 still healthy.
# Writes logs/acceptance.json on the data disk and prints a summary.
set -u
ROOT=/media/v/Data/recordian-decider-trial
OUT=$ROOT/logs/acceptance.json
date -u +%Y-%m-%dT%H:%M:%SZ
python3 - "$ROOT" << 'PY'
import json
import math
import subprocess
import sys
import time
import urllib.request

ROOT = sys.argv[1]
BASE = "http://192.168.5.111:42071"
MAX_MS = 350.0

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
# New sentences: not in the warmup set, not in the earlier probe/smoke/acceptance sets.
CASES = [
    {"id": "fresh2-tool-dictation-plugin", "surface": "jev", "heard": "jev", "context": "",
     "focus": "把jev的插件目录清空以后语音输入又恢复正常了", "expect": "tool"},
    {"id": "fresh2-person-review-owner", "surface": "Jeff", "heard": "Jeff", "context": "",
     "focus": "这次复盘是Jeff主讲的，后面有问题可以直接找他", "expect": "person"},
]


def payload(case):
    lines = []
    if case["context"]:
        lines.append("上一段：" + case["context"])
    lines.append(case["focus"])
    lines.append("只判断这个跨度：「%s」" % case["surface"])
    return {"state": "\n".join(lines),
            "questions": {"role": {"type": "choice",
                                   "instructions": INSTR.format(surface=case["surface"], heard=case["heard"]),
                                   "criteria": dict(CRITERIA)}}}


def call(case):
    body = json.dumps(payload(case), ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/v1/systemone", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw, status = resp.read(), resp.status
    except Exception as exc:
        return {"id": case["id"], "ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                "ms": round((time.perf_counter() - t0) * 1000, 3), "state": payload(case)["state"],
                "expected_choice": case["expect"]}
    ms = round((time.perf_counter() - t0) * 1000, 3)
    parsed = json.loads(raw)
    ans = (parsed.get("answers") or {}).get("role") or {}
    probs = ans.get("probabilities") or {}
    problems = []
    vals = []
    for key, value in probs.items():
        try:
            vals.append(float(value))
        except (TypeError, ValueError):
            problems.append("probability %r for %r is not a number" % (value, key))
    if status != 200:
        problems.append("http %s" % status)
    if ans.get("type") != "choice":
        problems.append("type %r" % ans.get("type"))
    if set(probs) != set(CRITERIA):
        problems.append("labels %r" % sorted(probs))
    if vals:
        if not all(math.isfinite(v) for v in vals):
            problems.append("non-finite probability")
        if any(not 0.0 <= v <= 1.0 for v in vals):
            problems.append("probability outside [0, 1]")
        if abs(sum(vals) - 1.0) > 0.02:
            problems.append("mass %.6f" % sum(vals))
        finite = [v for v in vals if math.isfinite(v)]
        top = max(finite) if finite else None
        choice = ans.get("choice")
        if choice not in probs:
            problems.append("choice %r is not a label" % choice)
        elif top is not None and float(probs.get(choice, float("-inf"))) < top - 1e-9:
            problems.append("choice %r is not the argmax" % choice)
    else:
        problems.append("no probabilities")
    if ms > MAX_MS:
        problems.append("slow %.3fms > %.0fms" % (ms, MAX_MS))
    return {"id": case["id"], "ok": not problems, "problems": problems, "http": status, "ms": ms,
            "state": payload(case)["state"], "expected_choice": case["expect"],
            "choice_matches_expectation": ans.get("choice") == case["expect"],
            "choice": ans.get("choice"), "top": (max(vals) if vals else None),
            "probabilities": probs, "confidence": ans.get("confidence"),
            "usage": parsed.get("usage"), "model": parsed.get("model"), "response_raw": parsed}


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def health(url):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return {"http": resp.status, "body": resp.read().decode()[:400],
                    "ms": round((time.perf_counter() - t0) * 1000, 3)}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


report = {
    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "max_ms": MAX_MS,
    "health_before": health(BASE + "/health"),
    "semif_42032": health("http://192.168.5.111:42032/health"),
    "requests": [call(c) for c in CASES],
    "stats_after": get(BASE + "/stats"),
    "container": {
        # no .State.Health here: the trial container has no HEALTHCHECK, so asking for it makes
        # docker inspect fail and would leave the OOM verdict without any evidence.
        "inspect": sh("docker inspect recordian-decider-trial --format "
                      "'STATUS={{.State.Status}} EXIT={{.State.ExitCode}} OOM={{.State.OOMKilled}} "
                      "RESTARTS={{.RestartCount}} STARTED={{.State.StartedAt}}'"),
        "memory": sh("docker stats --no-stream --format '{{.MemUsage}} {{.CPUPerc}} {{.PIDs}}' recordian-decider-trial"),
        "ports": sh("docker ps --filter name=^recordian-decider-trial$ --format '{{.Ports}}'"),
        "bind_sample": sh("ss -lntH | awk '$4 ~ /:42071$/ {print $4}' | sort -u | tr '\\n' ' '"),
        "restart_policy": sh("docker inspect recordian-decider-trial --format '{{.HostConfig.RestartPolicy.Name}}'"),
    },
}
try:
    report["warmup_report"] = json.load(open(ROOT + "/logs/warmup-report.json"))
except Exception as exc:
    report["warmup_report"] = {"error": "%s: %s" % (type(exc).__name__, exc)}

report["verdict"] = {
    "both_requests_ok": all(r["ok"] for r in report["requests"]),
    "both_under_350ms": all(r.get("ms", 1e9) <= MAX_MS for r in report["requests"]),
    "container_inspect_ok": "OOM=" in report["container"]["inspect"],
    "oom_killed": "OOM=true" in report["container"]["inspect"],
    "health_ok": report["health_before"].get("http") == 200,
    "semif_ok": report["semif_42032"].get("http") == 200,
    "warmup_ok": report["warmup_report"].get("status") == "ok",
    # informational only: the expected label is never a readiness/protocol criterion
    "choices_as_expected": all(r.get("choice_matches_expectation") for r in report["requests"]),
}
json.dump(report, open(ROOT + "/logs/acceptance.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
for r in report["requests"]:
    print("REQ", r["id"], "ok=", r["ok"], "http=", r.get("http"), "ms=", r.get("ms"),
          "choice=", r.get("choice"), "expected=", r.get("expected_choice"),
          "matches_expected=", r.get("choice_matches_expectation"), "top=", r.get("top"),
          "probs=", json.dumps(r.get("probabilities"), ensure_ascii=False), "problems=", r.get("problems"))
print("CONTAINER", report["container"]["inspect"], "|", report["container"]["memory"], "|", report["container"]["ports"])
print("SEMIF", report["semif_42032"].get("http"), "HEALTH", report["health_before"].get("http"))
print("VERDICT", json.dumps(report["verdict"], ensure_ascii=False))
PY
echo ACCEPTANCE_EXIT $?
