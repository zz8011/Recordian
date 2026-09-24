#!/bin/sh
# Build the Recordian fcitx5 streaming-composition addon into an isolated
# build tree (never installs or touches the user's running fcitx5).
# Usage: build.sh [build-dir]   (default: /tmp/recordian-commit-build)
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
build_dir="${1:-/tmp/recordian-commit-build}"
cmake -S "$here" -B "$build_dir" -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_dir" -j"$(nproc)"
echo "built: $build_dir/librecordian-commit.so"
