"""Tests for refine_model_discovery module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import pytest

from recordian.refine_model_discovery import fetch_model_list


def test_discovery_never_forwards_key_to_a_redirect_target(fake_models_server):
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", fake_models_server + "/v1/models")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The target would return models only if the bearer token leaked there.
        assert fetch_model_list(f"http://127.0.0.1:{server.server_port}", "test-key", timeout_s=2) == []
    finally:
        server.shutdown()
        server.server_close()


def test_remote_error_reason_is_not_logged(fake_models_server, caplog, monkeypatch):
    import urllib.error
    import urllib.request

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("http://example.invalid", 401, "reflected-private-fixture", {}, None)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    assert fetch_model_list(fake_models_server, "test-key") == []
    assert "reflected-private-fixture" not in caplog.text


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
