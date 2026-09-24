#!/bin/bash
# Read-only: find where the first-forward JIT/compile caches land inside the container.
set -u
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker exec recordian-decider-trial sh -c '
  echo "HOME=$HOME USER=$(id -un) UID=$(id -u)"
  echo "XDG_CACHE_HOME=$XDG_CACHE_HOME"
  echo "-- /root --"; ls -la /root 2>/dev/null | head -15
  echo "-- triton dirs on image --"; find / -xdev -maxdepth 6 -type d -name "*triton*" 2>/dev/null | head -10
  echo "-- miopen dirs --"; find / -xdev -maxdepth 6 -type d -name "*miopen*" 2>/dev/null | head -10
  echo "-- FILES CHANGED AFTER 20:35:40 (first forward window) --"
  find / -xdev -newermt "2026-09-24T20:35:40" -type f 2>/dev/null | grep -vE "^/proc|^/sys|^/tmp" | head -40
  echo "-- COUNT --"
  find / -xdev -newermt "2026-09-24T20:35:40" -type f 2>/dev/null | grep -vE "^/proc|^/sys" | wc -l
  echo "-- DIR SUMMARY --"
  find / -xdev -newermt "2026-09-24T20:35:40" -type f 2>/dev/null | grep -vE "^/proc|^/sys" | xargs -r -n1 dirname 2>/dev/null | sort | uniq -c | sort -rn | head -15
'
echo "CACHE_PROBE_DONE"
