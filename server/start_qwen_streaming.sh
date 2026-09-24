#!/bin/sh
# Official Qwen3-ASR streaming (vLLM). Recordian posts PCM to /api/chunk.
cd "$(dirname "$0")/.."
exec .venv-vllm/bin/python server/qwen_streaming_server.py
