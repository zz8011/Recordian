#!/bin/bash
set -u
ROOT=/media/v/Data/recordian-decider-trial/src/decider
echo "===== infer device pick ====="
grep -n -E "is_available|device|cuda|mps|cpu|version.cuda|hip" "$ROOT/decider/infer.py" | head -60
echo "===== serve startup ====="
sed -n '335,520p' "$ROOT/decider/serve.py"
echo "===== model fla ====="
grep -n -E "flash_linear|causal_conv|fla|is_available|cuda|device" "$ROOT/decider/model.py" | head -40
echo "===== engine_v2 graph ====="
grep -n -E "CUDAGraph|graph|is_available|device|warmup" "$ROOT/decider/engine_v2.py" | head -40
echo "===== systemone render choice ====="
sed -n '40,120p' "$ROOT/decider/systemone.py"
echo "REVIEW2_OK"
