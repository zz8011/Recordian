#!/bin/bash
# Read-only recon for the warmup-readiness change. Touches nothing.
set -u
ROOT=/media/v/Data/recordian-decider-trial
echo "UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "== OUR CONTAINERS =="
docker ps -a --filter name=recordian --format '{{.Names}} | {{.Status}} | {{.Ports}} | {{.Image}}' || true
echo "== NEIGHBOURS =="
docker ps --format '{{.Names}} | {{.Status}}' | grep -Ev 'recordian' || true
echo "== PORTS =="
ss -lntH 2>/dev/null | awk '{print $4}' | grep -E ':(42070|42071|42032)$' | sort || echo "none listening"
echo "== MEM =="
awk '/MemTotal|MemAvailable/ {print $1, $2, $3}' /proc/meminfo
echo "== HEALTH 42071 =="
curl -s -m 4 -o /dev/null -w 'code=%{http_code} time=%{time_total}\n' http://192.168.5.111:42071/health || echo "unreachable"
echo "== HEALTH 42032 SEMIF =="
curl -s -m 4 -o /dev/null -w 'code=%{http_code} time=%{time_total}\n' http://192.168.5.111:42032/health || echo "unreachable"
echo "== WEIGHTS FILES =="
ls -la "$ROOT/weights/decider-4b/" 2>/dev/null | head -20
echo "== WEIGHTS BIG FILES =="
find "$ROOT/weights/decider-4b" -maxdepth 1 -type f -size +100M -printf '%s %p\n' 2>/dev/null
echo "== DOWNLOAD LOG GATE LINES =="
grep -n -E 'WEIGHTS_OK|sha256|bytes' "$ROOT/logs/download.log" 2>/dev/null | tail -8
cat "$ROOT/logs/download.exit" 2>/dev/null || echo "no download.exit"
echo "== SRC HEAD =="
git -C "$ROOT/src/decider" rev-parse HEAD 2>/dev/null
git -C "$ROOT/src/decider" status --porcelain 2>/dev/null | head -20
echo "== SERVE FILES =="
ls -la "$ROOT/src/decider/src/decider/" 2>/dev/null || find "$ROOT/src/decider" -maxdepth 3 -name '*.py' -path '*decider*' | head -20
echo "== CACHE DIR =="
ls -la "$ROOT/cache" 2>/dev/null || echo "no cache dir yet"
echo "== ENV FILES IN LOGS =="
ls -la "$ROOT/logs/" 2>/dev/null
echo "== SERVICE DIR =="
ls -la /home/v/services/recordian-decider-trial/ 2>/dev/null || echo "service dir missing"
echo "RECON_DONE"
