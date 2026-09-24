#!/bin/bash
# Read-only state snapshot for the decider trial on 111.
set -u
ROOT=/media/v/Data/recordian-decider-trial
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "== MEM =="
awk '/MemTotal|MemAvailable/ {print $1, $2, $3}' /proc/meminfo
echo "== DISK =="
df -h "$ROOT" | tail -1
echo "== PORTS =="
ss -lntH 2>/dev/null | awk '{print $4}' | grep -E ':(42070|42071|42032)$' | sort || echo "none of 42070/42071/42032 listening"
echo "== CONTAINERS =="
docker ps -a --format '{{.Names}} | {{.Status}} | {{.Ports}}' 2>/dev/null | grep -E 'recordian|winnow|semif' || echo "no trial containers"
echo "== IMAGES =="
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' 2>/dev/null | grep -E 'qwen-retrieval|decider' || true
echo "== WEIGHTS =="
ls -la "$ROOT/weights/decider-4b/" 2>/dev/null
echo "== DOWNLOAD LOG =="
ls -la "$ROOT/logs/" 2>/dev/null
echo "-- download.log tail --"
tail -n 12 "$ROOT/logs/download.log" 2>/dev/null
echo "-- WEIGHTS_OK --"
grep -n "WEIGHTS_OK" "$ROOT/logs/download.log" 2>/dev/null || echo "WEIGHTS_OK not found"
echo "-- download.exit --"
cat "$ROOT/logs/download.exit" 2>/dev/null || echo "no download.exit"
echo "== SERVE-INNER PRESENT =="
ls -la "$ROOT/logs/serve-inner.sh" 2>/dev/null || echo "serve-inner.sh missing"
echo "== SRC/VENV =="
git -C "$ROOT/src/decider" rev-parse HEAD 2>/dev/null || echo "no src git"
ls "$ROOT/venv/bin/python" 2>/dev/null && echo "venv python present"
