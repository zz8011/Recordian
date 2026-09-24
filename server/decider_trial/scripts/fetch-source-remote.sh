#!/bin/bash
# Pin Mapika/decider and record the image Python stack. No model download.
set -u
ROOT=/media/v/Data/recordian-decider-trial
SVC=/home/v/services/recordian-decider-trial
mkdir -p "$ROOT/src" "$ROOT/weights" "$ROOT/logs" "$ROOT/venv" "$SVC"
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "$STAMP fetch-source start" | tee -a "$ROOT/logs/fetch-source.log"
docker run --rm -i --entrypoint /opt/venv/bin/python --network none \
  --memory 1g --cpus 2 \
  local/qwen-retrieval-gpustack:rocm - << 'PY'
import sys, importlib.util
print("python", sys.version.split()[0])
for name in ("torch", "transformers", "numpy", "triton", "flash_attn"):
    spec = importlib.util.find_spec(name)
    print("spec", name, bool(spec))
import torch, numpy, transformers
print("torch", torch.__version__)
print("numpy", numpy.__version__)
print("transformers", transformers.__version__)
try:
    import triton
    print("triton", triton.__version__)
except Exception as exc:
    print("triton_import", type(exc).__name__)
PY
if [ ! -d "$ROOT/src/decider/.git" ]; then
  rm -rf "$ROOT/src/decider"
  git clone --filter=blob:none https://github.com/Mapika/decider.git "$ROOT/src/decider" >>"$ROOT/logs/fetch-source.log" 2>&1
fi
git -C "$ROOT/src/decider" fetch --depth 1 origin 5f91c011f05fa4b685f0845281a0805a56eb0169 >>"$ROOT/logs/fetch-source.log" 2>&1 || \
  git -C "$ROOT/src/decider" fetch origin 5f91c011f05fa4b685f0845281a0805a56eb0169 >>"$ROOT/logs/fetch-source.log" 2>&1
git -C "$ROOT/src/decider" checkout --detach 5f91c011f05fa4b685f0845281a0805a56eb0169 >>"$ROOT/logs/fetch-source.log" 2>&1
echo "HEAD $(git -C "$ROOT/src/decider" rev-parse HEAD)"
echo "PYPROJECT_VERSION $(awk -F'\"' '/^version/ {print $2; exit}' "$ROOT/src/decider/pyproject.toml")"
echo "--- tree ---"
find "$ROOT/src/decider" -maxdepth 3 -type d -o -type f -name '*.py' -o -name 'pyproject.toml' -o -name '*.md' | head -200
echo "FETCH_SRC_OK"
