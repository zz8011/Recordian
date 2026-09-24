#!/bin/bash
set -u
ROOT=/media/v/Data/recordian-decider-trial/src/decider
echo "===== pyproject deps ====="
sed -n '1,160p' "$ROOT/pyproject.toml"
echo "===== SERVING device/fla ====="
grep -n -i -E "temperature|schema_first|cuda|rocm|hip|flash.linear|causal.conv|numpy|device|DECIDER_" "$ROOT/docs/SERVING.md" | head -80
echo "===== systemone signatures ====="
grep -n -E "def |temperature|probabilities|schema_first|criteria|choice" "$ROOT/decider/systemone.py" | head -80
echo "===== infer device ====="
grep -n -E "def |cuda|mps|cpu|temperature|letter|softmax|schema_first" "$ROOT/decider/infer.py" | head -80
echo "===== serve device ====="
grep -n -E "def |cuda|DECIDER_|temperature|schema|compile|fp8|device" "$ROOT/decider/serve.py" | head -100
echo "===== engine letter ====="
grep -n -E "letter|temperature|softmax|probabilities" "$ROOT/decider/engine.py" | head -40
echo "REVIEW_OK"
