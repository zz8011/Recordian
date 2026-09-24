#!/bin/bash
set -u
ROOT=/media/v/Data/recordian-decider-trial/src/decider
sed -n '110,220p' "$ROOT/decider/engine_v2.py"
echo "===== prompt letters ====="
grep -n -E "letter|option|softmax|temperature" "$ROOT/decider/prompt.py" | head -40
echo "===== assemble probabilities ====="
sed -n '120,200p' "$ROOT/decider/systemone.py"
echo "===== health route ====="
grep -n -E "health|/stats|@app" "$ROOT/decider/serve.py"
echo "REVIEW3_OK"
