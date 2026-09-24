#!/bin/bash
# Device proof + health + two real Chinese choice requests. Writes logs/probe.json.
set -u
ROOT=/media/v/Data/recordian-decider-trial
OUT=$ROOT/logs/probe.json
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "== DEVICE (inside container) =="
docker exec recordian-decider-trial /work/venv/bin/python - << 'PY'
import torch
print("TORCH", torch.__version__, "HIP", getattr(torch.version, "hip", None), "CUDA", getattr(torch.version, "cuda", None))
print("AVAILABLE", torch.cuda.is_available())
print("COUNT", torch.cuda.device_count())
print("NAME0", torch.cuda.get_device_name(0))
print("BACKEND", torch.cuda.get_device_properties(0).gcnArchName if hasattr(torch.cuda.get_device_properties(0), "gcnArchName") else "n/a")
a = torch.randn(8, 8, device="cuda", dtype=torch.bfloat16)
print("BF16_MATMUL_FINITE", bool(torch.isfinite(a @ a).all().item()))
PY
echo "== HTTP =="
python3 - << 'PY'
import json, time, urllib.request
BASE = "http://192.168.5.111:42071"
def get(path):
    t0 = time.perf_counter()
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, json.loads(r.read()), round((time.perf_counter() - t0) * 1000, 3)

out = {}
for path in ("/health", "/v1/models", "/stats"):
    try:
        st, body, ms = get(path)
        out[path] = {"http": st, "ms": ms, "body": body}
    except Exception as exc:
        out[path] = {"error": type(exc).__name__, "detail": str(exc)[:200]}

INSTR = ("判断句子中标记的「{surface}」在这个语境里指的是什么。只能从给定候选里选一个。"
         "拿不准就选“不清楚”。补充说明：用户的常用词里，jev 是这个用户的软件工具；语音识别经常把它误写成 {heard}。")
CRITERIA = {
    "tool": "这里指的是软件工具、程序或插件。",
    "person": "这里指的是一个人。",
    "unclear": "不清楚，或者以上都不是。",
}
CASES = [
    {"id": "cold-tool-memory", "surface": "jeff", "heard": "jeff",
     "focus": "打开jeff工具看一下这个项目的内存占用"},
    {"id": "warm-person-plan", "surface": "Jeff", "heard": "Jeff",
     "focus": "我和Jeff讨论了明天的发布方案"},
]
def body_for(c):
    return {
        "state": "%s\n只判断这个跨度：「%s」" % (c["focus"], c["surface"]),
        "questions": {"role": {"type": "choice",
            "instructions": INSTR.format(surface=c["surface"], heard=c["heard"]),
            "criteria": CRITERIA}},
    }
def call(c):
    payload = json.dumps(body_for(c), ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/v1/systemone", data=payload,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw, status = r.read(), r.status
    except Exception as exc:
        return {"id": c["id"], "ok": False, "error": type(exc).__name__,
                "detail": str(exc)[:300], "ms": round((time.perf_counter() - t0) * 1000, 3),
                "focus": c["focus"]}
    ms = round((time.perf_counter() - t0) * 1000, 3)
    parsed = json.loads(raw)
    ans = (parsed.get("answers") or {}).get("role") or {}
    return {"id": c["id"], "ok": status == 200 and ans.get("type") == "choice",
            "http": status, "ms": ms, "model": parsed.get("model"),
            "choice": ans.get("choice"), "probabilities": ans.get("probabilities"),
            "confidence": ans.get("confidence"), "usage": parsed.get("usage"),
            "focus": c["focus"], "answer_raw": ans}

out["choices"] = [call(c) for c in CASES]
json.dump(out, open("/media/v/Data/recordian-decider-trial/logs/probe.json", "w"),
          ensure_ascii=False, indent=2)
for c in out["choices"]:
    print("CHOICE", c["id"], "ok=", c.get("ok"), "http=", c.get("http"), "ms=", c.get("ms"),
          "choice=", c.get("choice"), "probs=", c.get("probabilities"), "err=", c.get("error"))
print("PROBE_WROTE")
PY
echo "PROBE_EXIT $?"
