#!/bin/bash
# Read-only: dump official sources to stdout for local review.
set -u
cd /media/v/Data/recordian-decider-trial/src/decider || exit 1
for f in src/decider/serve.py src/decider/engine_v2.py; do
  echo "@@@@@ BEGIN $f"
  cat "$f"
  echo "@@@@@ END $f"
done
