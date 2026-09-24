#!/bin/bash
# Read-only: current run timeline, in-container cache state, container config evidence.
set -u
ROOT=/media/v/Data/recordian-decider-trial
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "== START TIMELINE (docker logs -t) =="
docker logs -t recordian-decider-trial 2>&1 | head -12
echo "..."
docker logs -t recordian-decider-trial 2>&1 | tail -5
echo "== STATS =="
curl -s -m 5 http://192.168.5.111:42071/stats | head -c 1200; echo
echo "== CONTAINER MEM =="
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}} {{.PIDs}}' recordian-decider-trial 2>/dev/null
echo "== INSPECT (config) =="
docker inspect recordian-decider-trial --format '{{json .HostConfig}}' 2>/dev/null | head -c 2000; echo
docker inspect recordian-decider-trial --format 'ID={{.Id}} IMAGE={{.Image}} CREATED={{.Created}} STARTED={{.State.StartedAt}} OOM={{.State.OOMKilled}} RESTARTS={{.RestartCount}} EXIT={{.State.ExitCode}}' 2>/dev/null
echo "== IN-CONTAINER CACHE =="
docker exec recordian-decider-trial sh -c 'for d in /root/.triton /root/.cache/triton /root/.triton/cache /root/.cache; do echo "-- $d"; ls -la $d 2>/dev/null | head -8; done; du -sh /root/.triton 2>/dev/null'
echo "== TRITON ENV IN CONTAINER =="
docker exec recordian-decider-trial sh -c 'env | grep -iE "triton|miopen|hip|cache" || echo none'
echo "== TORCH/TRITON VERSIONS =="
docker exec recordian-decider-trial /work/venv/bin/python -c 'import torch,triton;print("torch",torch.__version__);print("triton",triton.__version__)' 2>&1 | tail -3
echo "== ROOT FREE =="
df -h /media/v/Data | tail -1
echo "RECON2_DONE"
