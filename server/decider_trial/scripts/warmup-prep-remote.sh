#!/bin/bash
# Phase 1 prep (no restart, nothing stopped): keep the evidence of the current container,
# back up the pre-warmup entry point, seed the JIT caches onto the data disk.
set -u
ROOT=/media/v/Data/recordian-decider-trial
mkdir -p "$ROOT/logs/rollback" "$ROOT/warmup" "$ROOT/cache"
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "== OLD CONTAINER EVIDENCE =="
OLD_ID=$(docker inspect recordian-decider-trial --format '{{.Id}}' 2>/dev/null || true)
if [ -n "$OLD_ID" ]; then
  docker inspect recordian-decider-trial > "$ROOT/logs/rollback/container-${OLD_ID:0:12}-$(date -u +%Y%m%dT%H%M%SZ).json"
  ls -la "$ROOT/logs/rollback/"
  docker inspect recordian-decider-trial \
    --format 'OLD ID={{.Id}} IMAGE={{.Image}} STATE={{.State.Status}} EXIT={{.State.ExitCode}} OOM={{.State.OOMKilled}} RESTARTS={{.RestartCount}} MEM={{.HostConfig.Memory}} CPUS={{.HostConfig.NanoCpus}} RESTART={{.HostConfig.RestartPolicy.Name}} RUNTIME={{.HostConfig.Runtime}} GROUPS={{.HostConfig.GroupAdd}} PORTS={{.HostConfig.PortBindings}} DEVICES={{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}'
else
  echo "no existing container"
fi
echo "== BACKUP PRE-WARMUP ENTRY POINT =="
if [ -s "$ROOT/logs/serve-inner.sh" ] && [ ! -f "$ROOT/logs/rollback/serve-inner.official-20260924.sh" ]; then
  cp -f "$ROOT/logs/serve-inner.sh" "$ROOT/logs/rollback/serve-inner.official-20260924.sh"
  echo "backed up serve-inner.sh"
else
  echo "backup already present or inner script missing: $(ls -la "$ROOT/logs/rollback/" | tr '\n' ' ')"
fi
echo "-- backup content --"
cat "$ROOT/logs/rollback/serve-inner.official-20260924.sh" 2>/dev/null | tail -4
echo "== CACHE SIZES IN RUNNING CONTAINER =="
docker exec recordian-decider-trial sh -c 'du -sh $HOME/.triton $HOME/.cache/comgr $HOME/.cache/miopen $HOME/.config/miopen 2>/dev/null; echo "HOME=$HOME"'
echo "== SEED CACHES TO /work/cache =="
docker exec recordian-decider-trial sh -c '
  set -e
  mkdir -p /work/cache/triton /work/cache/xdg/comgr /work/cache/miopen/kernels /work/cache/miopen/userdb /work/home
  [ -d "$HOME/.triton/cache" ] && cp -a "$HOME/.triton/cache/." /work/cache/triton/ || true
  [ -d "$HOME/.cache/comgr" ] && cp -a "$HOME/.cache/comgr/." /work/cache/xdg/comgr/ || true
  [ -d "$HOME/.cache/miopen" ] && cp -a "$HOME/.cache/miopen/." /work/cache/miopen/kernels/ || true
  [ -d "$HOME/.config/miopen" ] && cp -a "$HOME/.config/miopen/." /work/cache/miopen/userdb/ || true
  echo SEEDED
  du -sh /work/cache/triton /work/cache/xdg /work/cache/miopen
  find /work/cache -type f | wc -l
'
echo "PREP_DONE"
