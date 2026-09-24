#!/bin/bash
# Start the trial server only when memory, the real weight file, the source pin and the
# warmup wrapper are all in place.  Read-only w.r.t. everything it does not own.
# Gate 4 reads the whole 8.4 GB weight file (size + full sha256) on every start; a full read
# takes a few tens of seconds and is intentional -- nothing is trusted from a previous run.
set -u
ROOT=/media/v/Data/recordian-decider-trial
SRC_PIN=5f91c011f05fa4b685f0845281a0805a56eb0169
AVAIL_KB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ) MemAvailable_kB $AVAIL_KB"
# 20 GiB floor plus this container's 18 GiB cap.
if [ "$AVAIL_KB" -lt $((38 * 1024 * 1024)) ]; then
  echo "REFUSE low memory $AVAIL_KB"
  exit 3
fi
# Never clobber a healthy instance of our own container: refuse while /health answers.
OWN_HEALTH=$(curl -s -o /dev/null -w '%{http_code}' -m 4 http://192.168.5.111:42071/health 2>/dev/null || echo 000)
if [ "$OWN_HEALTH" = "200" ]; then
  echo "REFUSE own instance already healthy on 42071 (http $OWN_HEALTH)"
  exit 6
fi
if ss -lntH | awk '$4 ~ /:42071$/ {found=1} END {exit !found}'; then
  echo "REFUSE port 42071 busy"
  exit 4
fi
# Gate 1: the actual weight file, verified whole before every start: exact size plus the full
# sha256 of all 8.4 GB against the pin (tens of seconds, before docker run; no chunk sampling,
# no inode/mtime trust and no cached attestation).  The old WEIGHTS_OK grep only proved what a
# historical log once said.
if ! python3 "$ROOT/warmup/weights-pin-check.py"; then
  echo "REFUSE weight pin check failed"
  exit 5
fi
# Gate 2: the pinned source commit, so a locally edited serve.py cannot slip in.
HEAD=$(git -C "$ROOT/src/decider" rev-parse HEAD 2>/dev/null || echo unknown)
if [ "$HEAD" != "$SRC_PIN" ]; then
  echo "REFUSE source commit $HEAD != pin $SRC_PIN"
  exit 8
fi
for f in "$ROOT/warmup/serve_warmup.py" "$ROOT/logs/serve-inner.sh"; do
  if [ ! -s "$f" ]; then
    echo "REFUSE missing $f"
    exit 9
  fi
done
chmod +x "$ROOT/logs/serve-inner.sh"

# Keep the evidence of whatever container is being replaced, then remove only our own name.
mkdir -p "$ROOT/logs/rollback"
OLD_ID=$(docker inspect recordian-decider-trial --format '{{.Id}}' 2>/dev/null || true)
if [ -n "$OLD_ID" ]; then
  docker inspect recordian-decider-trial > "$ROOT/logs/rollback/container-${OLD_ID:0:12}-$(date -u +%Y%m%dT%H%M%SZ).json" 2>/dev/null || true
  echo "EVIDENCE old container $OLD_ID inspect saved to logs/rollback/"
fi
docker rm -f recordian-decider-trial >/dev/null 2>&1 || true

mkdir -p "$ROOT/cache/triton" "$ROOT/cache/xdg" "$ROOT/cache/miopen"
{
  echo "# docker run args used at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "docker run -d --name recordian-decider-trial --restart=no --runtime runc --device /dev/kfd --device /dev/dri --group-add 992 --group-add 44 --memory 18g --cpus 4 --pids-limit 512 -p 192.168.5.111:42071:42071 --entrypoint bash -v $ROOT:/work local/qwen-retrieval-gpustack:rocm /work/logs/serve-inner.sh"
} >> "$ROOT/logs/rollback/start-args.log"

docker run -d --name recordian-decider-trial \
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
  --entrypoint bash \
  -v "$ROOT:/work" \
  local/qwen-retrieval-gpustack:rocm \
  /work/logs/serve-inner.sh
echo SERVE_CONTAINER "$(docker ps -a --filter name=^recordian-decider-trial$ --format '{{.ID}} {{.Status}}')"
echo "SERVE_STARTED utc=$(date -u +%Y-%m-%dT%H:%M:%SZ) epoch=$(date -u +%s)"
