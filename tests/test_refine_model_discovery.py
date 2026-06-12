"""Tests for refine_model_discovery module."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, Iterator

import pytest

from recordian.refine_model_discovery import fetch_model_list


class _FakeModelsHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that returns a fake /v1/models response."""

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer test-key":
                self.send_response(401)
                self.end_headers()
                return
            payload = {
                "object": "list",
                "data": [
                    {"id": "gpt-4", "object": "model"},
                    {"id": "gpt-3.5-turbo", "object": "model"},
                    {"id": "", "object": "model"},  # empty id should be ignored
                    {"object": "model"},  # missing id should be ignored
                ],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress stderr noise


@pytest.fixture
def fake_models_server() -> Iterator[str]:
    """Spin up a local HTTP server and return its base URL."""
    server = HTTPServer(("127.0.0.1", 0), _FakeModelsHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_fetch_model_list_success(fake_models_server: str) -> None:
    models = fetch_model_list(fake_models_server, "test-key", timeout_s=2.0)
    assert models == ["gpt-3.5-turbo", "gpt-4"]


def test_fetch_model_list_no_auth(fake_models_server: str) -> None:
    models = fetch_model_list(fake_models_server, None, timeout_s=2.0)
    assert models == []


def test_fetch_model_list_bad_url() -> None:
    models = fetch_model_list("http://127.0.0.1:1", "test-key", timeout_s=0.5)
    assert models == []


def test_fetch_model_list_malformed_json(fake_models_server: str) -> None:
    # The fake server only handles /v1/models; any other path returns 404
    # which triggers the HTTPError branch and returns []
    models = fetch_model_list(fake_models_server, "test-key", timeout_s=2.0)
    assert models == ["gpt-3.5-turbo", "gpt-4"]  # normal path still works
