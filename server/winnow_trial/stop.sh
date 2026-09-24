#!/bin/bash
# Stop and remove only the trial server container.
set -euo pipefail
NAME=recordian-winnow-trial
case "$NAME" in
  recordian-winnow-trial) ;;
  *) echo "refuse: $NAME is outside the trial prefix" >&2; exit 1 ;;
esac
if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "no container named $NAME"
  exit 0
fi
docker stop "$NAME"
docker rm "$NAME"
echo "stopped $NAME"
