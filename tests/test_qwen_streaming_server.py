"""Unit tests for server/qwen_streaming_server.py.

The module must be importable without qwen_asr / vLLM / flask installed —
model dependencies load lazily inside main().
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).parent.parent / "server" / "qwen_streaming_server.py"


def _load_server_module():
    spec = importlib.util.spec_from_file_location("qwen_streaming_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def server_module():
    return _load_server_module()


def test_module_imports_without_model_deps(server_module):
    assert server_module.demo is None  # model handle stays lazy


def test_parser_defaults_loopback(server_module):
    args = server_module.build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.model  # non-empty default, no user-hardcoded absolute path
    assert not args.model.startswith("/home/")


def test_parser_accepts_model_host_port(server_module):
    args = server_module.build_parser().parse_args(
        ["--model", "/models/Qwen3-ASR-0.6B", "--host", "0.0.0.0", "--port", "9000"]
    )
    assert args.model == "/models/Qwen3-ASR-0.6B"
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_force_language_mapping(server_module):
    assert server_module._force_language("zh") == "Chinese"
    assert server_module._force_language("English") == "English"
    assert server_module._force_language("auto") is None
    assert server_module._force_language("") is None
    assert server_module._force_language("Japanese") == "Japanese"


def test_context_not_cut_to_80_chars(server_module):
    long_context = "热词" * 100  # 200 chars: the old code truncated this to 80
    resolved = server_module._resolve_context(long_context)
    assert resolved == long_context
    assert len(resolved) == 200


def test_context_budget_is_generous(server_module):
    over = "x" * (server_module.MAX_CONTEXT_CHARS + 10)
    resolved = server_module._resolve_context(over)
    assert len(resolved) == server_module.MAX_CONTEXT_CHARS
    assert server_module.MAX_CONTEXT_CHARS >= 1000
