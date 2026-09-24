#!/bin/bash
# Rollback for the decider trial.  Stops/removes this trial's containers only.
#   rollback-remote.sh            stop and remove the trial containers (data kept)
#   rollback-remote.sh --purge    also print (never run) the data-disk deletion commands
#   rollback-remote.sh --restore-official
#                                 start the container again with the *pre-warmup* entry point
#                                 (backed up at logs/rollback/serve-inner.official-20260924.sh)
set -u
ROOT=/media/v/Data/recordian-decider-trial
BACKUP=$ROOT/logs/rollback/serve-inner.official-20260924.sh
MODE=${1:-}
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ) mode=${MODE:-stop}"
docker stop recordian-decider-trial >/dev/null 2>&1 || true
docker rm -f recordian-decider-trial recordian-decider-trial-download recordian-decider-trial-warmupfailtest >/dev/null 2>&1 || true
echo "REMOVED trial containers: $(docker ps -a --filter name=recordian-decider-trial --format '{{.Names}}' | tr '\n' ' ')"
echo "PORT 42071: $(ss -lntH 2>/dev/null | awk '$4 ~ /:42071$/ {print $4}' | tr '\n' ' ')"
echo "NEIGHBOURS untouched: 42032 $(curl -s -o /dev/null -w '%{http_code}' -m 4 http://192.168.5.111:42032/health 2>/dev/null) 42070 $(curl -s -o /dev/null -w '%{http_code}' -m 4 http://192.168.5.111:42070/health 2>/dev/null)"

if [ "$MODE" = "--purge" ]; then
  cat <<'EOS'
Data kept (no deletion run by this script).  To purge this trial only, after checking that no
other container uses these directories:
  docker rm -f recordian-decider-trial recordian-decider-trial-download
  rm -rf /media/v/Data/recordian-decider-trial /home/v/services/recordian-decider-trial
Do NOT touch: image local/qwen-retrieval-gpustack:rocm, SemIf 42032, Winnow 42070 and its
directories, host ROCm/driver, production .env.
EOS
fi

if [ "$MODE" = "--restore-official" ]; then
  if [ ! -s "$BACKUP" ]; then
    echo "REFUSE no pre-warmup backup at $BACKUP"
    exit 5
  fi
  cp -f "$BACKUP" "$ROOT/logs/serve-inner.sh"
  chmod +x "$ROOT/logs/serve-inner.sh"
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
  echo "RESTORED official entry point (no warmup) from $BACKUP"
fi
