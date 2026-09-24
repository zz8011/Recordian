#!/bin/bash
# Pure-Python flash-linear-attention wheels on top of the image's ROCm Triton.
set -u
ROOT=/media/v/Data/recordian-decider-trial
LOG=$ROOT/logs/fla-install.log
echo $$ > "$ROOT/logs/fla-install.pid"
date -u +%Y-%m-%dT%H:%M:%SZ | tee "$LOG"
cat > "$ROOT/logs/fla_smoke.py" << 'PY'
import traceback
import torch
print("torch", torch.__version__, "hip", getattr(torch.version, "hip", None))
print("device", torch.cuda.get_device_name(0))
try:
    import fla
    print("fla", getattr(fla, "__version__", "?"), fla.__file__)
    from fla.layers import GatedLinearAttention
    layer = GatedLinearAttention(mode="chunk", hidden_size=64, num_heads=2).to(device="cuda", dtype=torch.bfloat16)
    x = torch.randn(1, 32, 64, device="cuda", dtype=torch.bfloat16)
    y = layer(x)
    if isinstance(y, tuple):
        y = y[0]
    torch.cuda.synchronize()
    print("fla_out", tuple(y.shape), str(y.dtype), str(y.device), bool(torch.isfinite(y).all().item()))
    print("FLA_SMOKE_OK")
except Exception:
    traceback.print_exc()
    raise SystemExit(3)
PY
docker run --rm \
  --entrypoint bash \
  --device /dev/kfd --device /dev/dri \
  --group-add 992 --group-add 44 \
  --memory 8g --cpus 4 \
  -v "$ROOT:/work" \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -e MAX_JOBS=4 \
  local/qwen-retrieval-gpustack:rocm \
  -lc 'set -eu
/work/venv/bin/python -m pip install --upgrade-strategy only-if-needed -i https://pypi.tuna.tsinghua.edu.cn/simple flash-linear-attention
/work/venv/bin/python -c "import importlib.util; print(\"causal_conv1d\", bool(importlib.util.find_spec(\"causal_conv1d\")))"
/work/venv/bin/python /work/logs/fla_smoke.py
' >> "$LOG" 2>&1
code=$?
echo "$code" > "$ROOT/logs/fla-install.exit"
echo FLA_EXIT "$code"
tail -30 "$LOG"
exit "$code"
