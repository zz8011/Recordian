#!/bin/bash
# Read-only: locate and size the official serve/engine sources.
set -u
ROOT=/media/v/Data/recordian-decider-trial
cd "$ROOT/src/decider" || exit 1
echo "== TREE src =="
find . -maxdepth 3 -name '*.py' -not -path './tests/*' -not -path './examples/*' -printf '%s %p\n' | sort -k2
echo "== PACKAGE =="
ls -la src/decider/ 2>/dev/null || ls -la decider/ 2>/dev/null
echo "== VERSION =="
grep -rn "version" pyproject.toml 2>/dev/null | head -5
echo "== LIFESPAN/HOOKS in serve.py =="
grep -n -E "lifespan|asynccontextmanager|yield|FastAPI\(|def health|@app|engine|seal|load_model|warmup|WARMUP" src/decider/serve.py 2>/dev/null | head -60
echo "SIZES_DONE"
