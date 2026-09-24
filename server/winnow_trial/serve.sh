#!/bin/bash
# Start the text-only HIP trial. Refuses to replace a healthy listener.
# Checks run before docker run; a new start always sha256sums the model file itself.
# --memory=28g is only the cgroup cap.
set -euo pipefail
TRIAL_ROOT=/media/v/Data/recordian-winnow-trial
NAME=recordian-winnow-trial
IMAGE=recordian-winnow-trial:toolchain
EXPECT_SIZE=12669646592
EXPECT_SHA=b710efc4c0d048ee61eed92c5fef5ce323a4d17e7c51f9f0533cc72ae50818ea
FLOOR=$((20 * 1024 * 1024 * 1024))
CONTEXT_RESERVE=$((6 * 1024 * 1024 * 1024))

case "$NAME" in
  recordian-winnow-trial) ;;
  *) echo "refuse: container name is outside recordian-winnow-trial" >&2; exit 1 ;;
esac

ROOT=$(readlink -f "$TRIAL_ROOT")
if [ "$ROOT" != "$TRIAL_ROOT" ]; then
  echo "refuse: trial root resolves to $ROOT" >&2
  exit 1
fi
MODEL=$ROOT/models/gguf/Winnow-12B-Q8_0.gguf
RECEIPT=$MODEL.sha256receipt

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  state=$(docker inspect -f '{{.State.Status}}' "$NAME")
  cid=$(docker inspect -f '{{.Id}}' "$NAME")
  echo "existing $NAME status=$state id=$cid"
  if [ "$state" = "running" ] && curl -fsS -m 5 http://127.0.0.1:42070/health | grep -q '"status":"ok"'; then
    echo "healthy listener on 127.0.0.1:42070; not recreating"
    exit 0
  fi
  echo "not a healthy listener; left in place. stop.sh removes only $NAME." >&2
  exit 1
fi

avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
avail=$((avail_kb * 1024))
need=$((FLOOR + EXPECT_SIZE + CONTEXT_RESERVE))
if [ "$avail" -lt "$FLOOR" ] || [ "$avail" -lt "$need" ]; then
  echo "refuse: MemAvailable=${avail} bytes; need >= ${need} (20GiB remaining floor + model ${EXPECT_SIZE} + 6GiB context reserve). The 28g cgroup cap is not this check." >&2
  exit 1
fi

if [ ! -f "$MODEL" ]; then
  echo "refuse: model file missing: $MODEL" >&2
  exit 1
fi
sz=$(stat -c %s "$MODEL")
if [ "$sz" != "$EXPECT_SIZE" ]; then
  echo "refuse: model size ${sz} != ${EXPECT_SIZE}" >&2
  exit 1
fi
# Always hash the real bytes before a new container starts. The receipt records a
# past check for download_q8.sh's resume path; it is never accepted as proof of the
# current file, because a same-size replacement with a stale receipt would skip the
# checksum entirely. One sequential read of the 12.7GB file is accepted here.
echo "hashing $MODEL before start"
got=$(sha256sum "$MODEL" | awk '{print $1}')
if [ "$got" != "$EXPECT_SHA" ]; then
  echo "refuse: sha256 ${got} != ${EXPECT_SHA}" >&2
  exit 1
fi
echo "weight file verified: sha256 ${got}"
# Refresh the receipt from the bytes just read. A write failure is not fatal: the
# start decision above did not depend on this file.
printf 'sha256=%s\nsize=%s\npath=%s\n' "$EXPECT_SHA" "$EXPECT_SIZE" "$MODEL" > "$RECEIPT" 2>/dev/null || \
  echo "note: could not refresh receipt $RECEIPT (ignored)" >&2

exec docker run -d --name "$NAME" \
  --restart=no \
  --runtime amd \
  --memory=28g --memory-swap=28g \
  --shm-size=1g \
  --device /dev/kfd --device /dev/dri \
  --group-add 992 --group-add 44 \
  -e HIP_VISIBLE_DEVICES=0 \
  -e AMD_VISIBLE_DEVICES=0 \
  -e HSA_NO_SCRATCH_RECLAIM=1 \
  -e WINNOW_CONTEXT=4096 \
  -e WINNOW_HEAD=selected \
  -e WINNOW_CACHE=f16 \
  -e WINNOW_PIPELINE=optimized \
  -e WINNOW_MEMORY=exclusive \
  -e WINNOW_PARALLEL=1 \
  -e WINNOW_BATCH=512 \
  -e WINNOW_UBATCH=512 \
  -p 127.0.0.1:42070:42070 \
  -p 192.168.5.111:42070:42070 \
  -v "$ROOT:/work" \
  --entrypoint /work/build/bin/winnow-server \
  "$IMAGE" \
  --model /work/models/gguf/Winnow-12B-Q8_0.gguf \
  --alias Winnow-12B \
  --ctx-size 4096 \
  --parallel 1 \
  --n-gpu-layers 999 \
  --fit off \
  --flash-attn on \
  --cache-type-k f16 \
  --cache-type-v f16 \
  --no-context-shift \
  --lazy-mode off \
  --split-mode none \
  --override-tensor '^(token_embd|per_layer_token_embd)\.weight$=ROCm0' \
  --batch-size 512 \
  --ubatch-size 512 \
  --threads 4 \
  --host 0.0.0.0 \
  --port 42070 \
  --jinja \
  --reasoning off \
  --no-warmup \
  --cache-ram 0 \
  --cors-origins ""
