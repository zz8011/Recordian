#!/bin/bash
# Bounded readiness poll for the trial port.  One call never waits longer than the window
# (<= 28 s) and nothing runs in the background: call it again for another window.
#   readiness-poll-remote.sh <start_epoch> [window_s]
set -u
START=${1:-$(date -u +%s)}
WINDOW=${2:-26}
if [ "$WINDOW" -gt 28 ]; then
  WINDOW=28
fi
DEADLINE=$((START + WINDOW))
echo "POLL start_epoch=$START window_s=$WINDOW now_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
while :; do
  NOW=$(date -u +%s)
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://192.168.5.111:42071/health 2>/dev/null || echo 000)
  LISTEN=$(ss -lntH 2>/dev/null | awk '$4 ~ /:42071$/ {print $4; exit}')
  printf 'T+%3ds health_http=%s listen=%s\n' "$((NOW - START))" "$CODE" "${LISTEN:-none}"
  if [ "$CODE" = "200" ]; then
    echo "READY elapsed_s=$((NOW - START))"
    exit 0
  fi
  if [ "$NOW" -ge "$DEADLINE" ]; then
    echo "NOT_READY_YET elapsed_s=$((NOW - START))"
    exit 10
  fi
  sleep 2
done
