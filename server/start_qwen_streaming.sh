#!/bin/sh
# Official Qwen3-ASR streaming (vLLM). Recordian posts PCM to /api/chunk.
# Absolute checkout path: a relative models/... string is a valid HF repo id.
# ASR_MODEL_PATH replaces that default; a later caller --model still wins.
cd "$(dirname "$0")/.." || exit
root=$(pwd) || exit
exec .venv-vllm/bin/python server/qwen_streaming_server.py \
    --model "${ASR_MODEL_PATH:-$root/models/Qwen3-ASR-0.6B}" \
    "$@"
