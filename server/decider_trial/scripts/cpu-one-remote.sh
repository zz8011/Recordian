#!/bin/bash
# One CPU eager sentence for a numeric contrast. Skips when memory is tight or the call exceeds 180s.
set -u
ROOT=/media/v/Data/recordian-decider-trial
AVAIL_KB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ) MemAvailable_kB $AVAIL_KB"
# GPU server may already hold up to 18 GiB. Require 20 GiB free plus another 12 GiB for this process.
if [ "$AVAIL_KB" -lt $((32 * 1024 * 1024)) ]; then
  echo "CPU_SKIP memory $AVAIL_KB"
  echo '{"skipped": true, "reason": "MemAvailable below 32GiB while GPU server is up"}' > "$ROOT/logs/cpu-one.json"
  exit 0
fi
docker rm -f recordian-decider-trial-cpu >/dev/null 2>&1 || true
docker run --rm --name recordian-decider-trial-cpu \
  --restart=no \
  --memory 12g --cpus 4 \
  --entrypoint bash \
  -v "$ROOT:/work" \
  -e DECIDER_DEVICE=cpu \
  -e OMP_NUM_THREADS=4 \
  -e MKL_NUM_THREADS=4 \
  local/qwen-retrieval-gpustack:rocm \
  -lc '/work/venv/bin/python - << "PY"
import json, time
from decider.infer import Decider
t0=time.perf_counter()
d=Decider("/work/weights/decider-4b", device="cpu", use_graphs=False)
load_s=time.perf_counter()-t0
state="打开jeff工具检查这个项目\n只判断这个跨度：「jeff」"
questions={"role":{"type":"choice","instructions":"判断句子中标记的「jeff」在这个语境里指的是什么。只能从给定候选里选一个。拿不准就选“不清楚”。补充说明：用户的常用词里，jev 是这个用户的软件工具；语音识别经常把它误写成 jeff。","criteria":{"tool":"这里指的是软件工具、程序或插件。","person":"这里指的是一个人。","unclear":"不清楚，或者以上都不是。"}}}
t1=time.perf_counter()
out=d.system_one(state, questions)
infer_s=time.perf_counter()-t1
json.dump({"skipped": False, "load_s": round(load_s,3), "infer_s": round(infer_s,3), "answer": out}, open("/work/logs/cpu-one.json","w"), ensure_ascii=False, indent=2)
print("CPU_ONE_OK", round(load_s,3), round(infer_s,3))
PY'
code=$?
echo CPU_EXIT "$code"
if [ "$code" != 0 ]; then
  echo '{"skipped": true, "reason": "cpu process failed or timed out"}' > "$ROOT/logs/cpu-one.json"
fi
exit 0
