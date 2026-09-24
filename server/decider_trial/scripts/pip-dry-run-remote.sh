#!/bin/bash
set -u
LOG=/media/v/Data/recordian-decider-trial/logs/pip-dry-run.log
mkdir -p /media/v/Data/recordian-decider-trial/logs /media/v/Data/recordian-decider-trial/venv
date -u +%Y-%m-%dT%H:%M:%SZ | tee "$LOG"
docker run --rm -i \
  --entrypoint /opt/venv/bin/python \
  --memory 6g --cpus 4 \
  -v /media/v/Data/recordian-decider-trial:/work \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -e MAX_JOBS=4 \
  local/qwen-retrieval-gpustack:rocm - << 'PY' | tee -a /media/v/Data/recordian-decider-trial/logs/pip-dry-run.log
import os, subprocess, sys
py = sys.executable
env = os.environ.copy()
env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
cmd = [py, "-m", "pip", "install", "--dry-run", "--upgrade-strategy", "only-if-needed",
       "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
       "transformers>=5", "numpy<2", "fastapi", "uvicorn", "httpx", "jinja2", "huggingface_hub"]
print("CMD", " ".join(cmd), flush=True)
proc = subprocess.run(cmd, env=env)
print("DRY_EXIT", proc.returncode)
PY
echo DRY_SCRIPT_EXIT $?
