#!/bin/bash
set -u
ROOT=/media/v/Data/recordian-decider-trial
echo "===== pyvenv ====="
sed -n '1,20p' "$ROOT/venv/pyvenv.cfg"
echo "===== python ====="
readlink -f "$ROOT/venv/bin/python"
ls "$ROOT/venv/lib"
docker run --rm -i \
  --entrypoint bash \
  --device /dev/kfd --device /dev/dri \
  --group-add 992 --group-add 44 \
  --memory 4g --cpus 2 \
  -v "$ROOT:/work" \
  local/qwen-retrieval-gpustack:rocm \
  -lc 'set -eu
SITE=$(/work/venv/bin/python -c "import site; print(site.getsitepackages()[0])")
echo "venv_site $SITE"
BASE=$(/opt/venv/bin/python -c "import site; print(site.getsitepackages()[0])")
echo "base_site $BASE"
printf "%s\n" "$BASE" > "$SITE/rocm-image-venv.pth"
/work/venv/bin/python -c "import torch,numpy,transformers,decider; print(torch.__file__); print(torch.__version__); print(getattr(torch.version,\"hip\",None)); print(numpy.__version__, numpy.__file__); print(transformers.__version__); print(decider.__file__); print(\"cuda\", torch.cuda.is_available())"
echo "===== fla dry ====="
/work/venv/bin/python -m pip install --dry-run --upgrade-strategy only-if-needed -i https://pypi.tuna.tsinghua.edu.cn/simple flash-linear-attention
'
echo FIX_EXIT $?
