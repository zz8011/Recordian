#!/bin/bash
# Start the trial through the gated start script and take the first readiness samples.
# Callers repeat logs/readiness-poll-remote.sh <start_epoch> 28 for further bounded windows.
set -u
R=/media/v/Data/recordian-decider-trial
docker rm -f recordian-decider-trial-warmupfailtest >/dev/null 2>&1 || true
echo "== PORT BEFORE: [$(ss -lntH 2>/dev/null | awk '$4 ~ /:42071$/ {print $4}' | sort -u | tr '\n' ' ')]"
echo "== START SCRIPT =="
bash "$R/logs/serve-remote.sh"
START=$(date -u +%s)
echo "START_EPOCH=$START"
sleep 3
echo "== T+3 SAMPLE =="
echo "health=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://192.168.5.111:42071/health 2>/dev/null || echo 000)"
echo "listen=[$(ss -lntH 2>/dev/null | awk '$4 ~ /:42071$/ {print $4}' | sort -u | tr '\n' ' ')]"
bash "$R/logs/readiness-poll-remote.sh" "$START" 28 || true
echo "START_AND_POLL_DONE start_epoch=$START"
