#!/bin/bash
# In-container entry point: official decider.serve behind the trial readiness wrapper.
# Eager only: DECIDER_WARMUP=0 seals the engine, no CUDA graphs, no torch.compile, no FP8.
set -u
export DECIDER_MODEL=/work/weights/decider-4b
export DECIDER_DEVICE=cuda
export DECIDER_COMPILE=0
export DECIDER_FP8=0
export DECIDER_WARMUP=0
export DECIDER_SCHEMA_CACHE=0
export DECIDER_TEMPERATURE=1.935
export DECIDER_TOKENIZE_THREADS=4
export DECIDER_MAX_BATCH=1
export DECIDER_MAX_STATE_TOKENS=2048
export DECIDER_SHARED=0
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1

# Kernel/JIT caches live on the data disk, not in the container layer, so a restart reuses
# them: triton (gated-delta-rule kernels), comgr (LLVM code objects), MIOpen find-db + kernels.
# Every cache is pinned by its own variable, so HOME is left exactly as the image sets it
# (the official pre-warmup entry point never touched it either) and nothing depends on $HOME.
export XDG_CACHE_HOME=/work/cache/xdg
export TRITON_CACHE_DIR=/work/cache/triton
export MIOPEN_USER_DB_PATH=/work/cache/miopen/userdb
export MIOPEN_CUSTOM_CACHE_DIR=/work/cache/miopen/kernels
export DECIDER_WARMUP_REPORT=${DECIDER_WARMUP_REPORT:-/work/logs/warmup-report.json}
export DECIDER_APP=${DECIDER_APP:-serve_warmup:app}
mkdir -p "$XDG_CACHE_HOME" "$TRITON_CACHE_DIR" "$MIOPEN_USER_DB_PATH" "$MIOPEN_CUSTOM_CACHE_DIR" /work/logs

RUN_START=$(date -u +%s)
echo "[serve] inner start utc=$(date -u +%Y-%m-%dT%H:%M:%SZ) epoch=$RUN_START app=$DECIDER_APP report=$DECIDER_WARMUP_REPORT"
cd /work/src/decider || exit 1
/work/venv/bin/python -m uvicorn "$DECIDER_APP" --app-dir /work/warmup \
  --host 0.0.0.0 --port 42071 --workers 1
rc=$?
echo "[serve] uvicorn exit code $rc utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

# uvicorn exits 0 even when the lifespan (and so the warmup) failed, which would make the
# container exit code lie.  Read this run's warmup verdict and turn a failure into exit 7.
REPORT=$DECIDER_WARMUP_REPORT
if [ -f "$REPORT" ]; then
  MTIME=$(stat -c %Y "$REPORT" 2>/dev/null || echo 0)
  if [ "$MTIME" -ge "$RUN_START" ]; then
    STATUS=$(/work/venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", "unknown"))' "$REPORT" 2>/dev/null || echo unknown)
    echo "[serve] this run warmup status=$STATUS (report mtime $MTIME)"
    if [ "$STATUS" = "failed" ]; then
      echo "[serve] REFUSING to serve: warmup failed, exiting 7"
      exit 7
    fi
  fi
fi
echo "[serve] stopped cleanly"
exit 0
