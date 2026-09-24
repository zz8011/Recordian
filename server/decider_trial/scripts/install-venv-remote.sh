#!/bin/bash
# Isolated venv on the data disk. Reuses the image ROCm torch via --system-site-packages.
set -u
ROOT=/media/v/Data/recordian-decider-trial
LOG=$ROOT/logs/pip-install.log
PIDF=$ROOT/logs/pip-install.pid
EXITF=$ROOT/logs/pip-install.exit
mkdir -p "$ROOT/logs" "$ROOT/venv"
echo $$ > "$PIDF"
date -u +%Y-%m-%dT%H:%M:%SZ | tee "$LOG"
cat > "$ROOT/logs/check_torch.py" << 'PY'
import importlib.metadata as md
import torch
import numpy
import transformers
print("torch", torch.__version__)
print("hip", getattr(torch.version, "hip", None))
print("numpy", numpy.__version__)
print("transformers", transformers.__version__)
print("decider", md.version("decider-ai"))
assert "rocm" in torch.__version__, torch.__version__
assert torch.cuda.is_available()
print("INSTALL_TORCH_OK")
PY
docker run --rm \
  --entrypoint bash \
  --memory 8g --cpus 4 \
  -v "$ROOT:/work" \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -e MAX_JOBS=4 \
  -e CMAKE_BUILD_PARALLEL_LEVEL=4 \
  -e HF_HUB_DISABLE_TELEMETRY=1 \
  local/qwen-retrieval-gpustack:rocm \
  -lc 'set -eu
if [ ! -x /work/venv/bin/python ]; then
  /opt/venv/bin/python -m venv /work/venv --system-site-packages
fi
# venv follows /usr/bin/python3.12, so --system-site-packages does not see /opt/venv.
BASE=$(/opt/venv/bin/python -c "import site; print(site.getsitepackages()[0])")
SITE=$(/work/venv/bin/python -c "import site; print(site.getsitepackages()[0])")
printf "%s\n" "$BASE" > "$SITE/rocm-image-venv.pth"
/work/venv/bin/python -m pip install --upgrade-strategy only-if-needed -i https://pypi.tuna.tsinghua.edu.cn/simple "transformers>=5" "numpy<2" fastapi uvicorn httpx jinja2 huggingface_hub
/work/venv/bin/python -m pip install --no-deps -e /work/src/decider
echo "===== versions ====="
/work/venv/bin/python /work/logs/check_torch.py
echo "===== fla dry-run ====="
/work/venv/bin/python -m pip install --dry-run --upgrade-strategy only-if-needed -i https://pypi.tuna.tsinghua.edu.cn/simple flash-linear-attention
' >> "$LOG" 2>&1
code=$?
echo "$code" > "$EXITF"
echo INSTALL_EXIT "$code"
tail -50 "$LOG"
exit "$code"
