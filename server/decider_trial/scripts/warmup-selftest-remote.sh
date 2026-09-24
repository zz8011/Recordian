#!/bin/bash
# One-off proof that a *failing* warmup keeps the trial port unready.
#
# It runs the same container image, mounts and inner script as the trial, but loads
# serve_warmup_selftest:app: case 1 runs a real forward, case 2 is rejected by the official
# criteria validation, so the warmup fails inside the lifespan.  Expected: uvicorn never binds
# 42071, /health is never 200, the process exits (the inner script turns the failed verdict
# into exit 7).  The test container is removed afterwards; the trial container must be stopped.
set -u
ROOT=/media/v/Data/recordian-decider-trial
NAME=recordian-decider-trial-warmupfailtest
LOG=$ROOT/logs/warmup-selftest-container.log
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
if curl -s -o /dev/null -w '%{http_code}' -m 3 http://192.168.5.111:42071/health 2>/dev/null | grep -q 200; then
  echo "REFUSE 42071 is healthy; stop the trial container before the selftest"
  exit 6
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
START=$(date -u +%s)
docker run -d --name "$NAME" \
  --restart=no \
  --runtime runc \
  --device /dev/kfd \
  --device /dev/dri \
  --group-add 992 \
  --group-add 44 \
  --memory 18g \
  --cpus 4 \
  --pids-limit 512 \
  -p 192.168.5.111:42071:42071 \
  -e DECIDER_APP=serve_warmup_selftest:app \
  -e DECIDER_WARMUP_REPORT=/work/logs/warmup-report-selftest.json \
  --entrypoint bash \
  -v "$ROOT:/work" \
  local/qwen-retrieval-gpustack:rocm \
  /work/logs/serve-inner.sh
echo "SELFTEST_STARTED epoch=$START"

# Bounded windows only; the caller repeats the poll command if needed.
bash "$ROOT/logs/readiness-poll-remote.sh" "$START" 28 || true
echo "== CONTAINER STATE =="
docker inspect "$NAME" --format 'STATUS={{.State.Status}} EXIT={{.State.ExitCode}} OOM={{.State.OOMKilled}} STARTED={{.State.StartedAt}} FINISHED={{.State.FinishedAt}}' 2>/dev/null || echo "inspect failed"
echo "== LOG (kept at $LOG) =="
docker logs "$NAME" > "$LOG" 2>&1 || true
grep -nE "warmup|Application startup failed|Uvicorn running|Error|error|exit code|REFUSING" "$LOG" | tail -25
docker rm -f "$NAME" >/dev/null 2>&1 || true
echo "SELFTEST_DONE removed=$NAME"
