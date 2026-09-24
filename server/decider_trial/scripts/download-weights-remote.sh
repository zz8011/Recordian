#!/bin/bash
set -u
ROOT=/media/v/Data/recordian-decider-trial
mkdir -p "$ROOT/logs" "$ROOT/weights"
# The inner script is copied onto the mount by the caller before this runs.
chmod +x "$ROOT/logs/download-inner.sh"
rm -f "$ROOT/logs/download.exit"
docker rm -f recordian-decider-trial-download >/dev/null 2>&1 || true
docker run -d --name recordian-decider-trial-download \
  --restart=no \
  --memory 4g --cpus 4 \
  --entrypoint bash \
  -v "$ROOT:/work" \
  local/qwen-retrieval-gpustack:rocm \
  -lc 'set -o pipefail; bash /work/logs/download-inner.sh > /work/logs/download.log 2>&1; code=$?; if [ ! -f /work/logs/download.exit ]; then echo $code > /work/logs/download.exit; fi; exit $code'
echo DOWNLOAD_CONTAINER "$(docker ps -a --filter name=recordian-decider-trial-download --format '{{.ID}} {{.Status}}')"
echo DOWNLOAD_STARTED
