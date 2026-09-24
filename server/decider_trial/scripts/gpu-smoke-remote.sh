#!/bin/bash
# One-shot ROCm smoke inside the existing retrieval image.
# Does not touch running containers. Numeric GIDs avoid the image's missing "render" group name.
set -u
NAME=recordian-decider-trial-smoke
IMAGE=local/qwen-retrieval-gpustack:rocm
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run --rm -i --name "$NAME" \
  --entrypoint /opt/venv/bin/python \
  --runtime runc \
  --device /dev/kfd \
  --device /dev/dri \
  --group-add 992 \
  --group-add 44 \
  --memory 4g \
  --cpus 4 \
  --pids-limit 256 \
  -e OMP_NUM_THREADS=4 \
  -e MKL_NUM_THREADS=4 \
  -e HF_HUB_DISABLE_TELEMETRY=1 \
  "$IMAGE" - << 'PY'
import os
print("uid", os.getuid())
print("groups", os.getgroups())
import torch
print("torch", torch.__version__)
print("hip", getattr(torch.version, "hip", None))
print("cuda", getattr(torch.version, "cuda", None))
ok = torch.cuda.is_available()
print("is_available", ok)
print("device_count", torch.cuda.device_count())
if not ok:
    raise SystemExit(2)
props = torch.cuda.get_device_properties(0)
print("name", props.name)
print("gcn", getattr(props, "gcnArchName", None))
print("total_mem", props.total_memory)
a = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
b = torch.randn(1024, 1024, device="cuda", dtype=torch.bfloat16)
c = a @ b
torch.cuda.synchronize()
print("bf16_device", str(c.device))
print("bf16_dtype", str(c.dtype))
print("bf16_finite", bool(torch.isfinite(c).all().item()))
print("bf16_sample", float(c.reshape(-1)[0]))
print("SMOKE_OK")
PY
echo "SMOKE_EXIT $?"
