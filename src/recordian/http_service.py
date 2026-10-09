"""Bounded HTTP transport for Recordian's local Flask model services.

The listener accepts plain sockets. TLS handshakes run only after worker
admission, and headers plus body share a receive deadline. Once a complete
body has been received, inference has no transport time limit. Authentication,
Origin/Host checks and request byte limits remain the application's job.
"""

from __future__ import annotations

import io
import math
import socket
import ssl
import threading
import time
from typing import Any

from werkzeug.exceptions import BadRequest, ClientDisconnected, RequestTimeout
from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler


class _ReceiveDeadline:
    def __init__(self, connection: socket.socket, seconds: float) -> None:
        self.connection = connection
        self.expires: float | None = time.monotonic() + seconds

    def arm(self) -> None:
        if self.expires is not None:
            remaining = self.expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP receive deadline exceeded")
            self.connection.settimeout(remaining)

    def complete(self) -> None:
        self.expires = None
        # An idle write timeout bounds slow readers, without timing inference.
        self.connection.settimeout(3.0)


class _SocketInput(io.RawIOBase):
    def __init__(self, connection: socket.socket, deadline: _ReceiveDeadline) -> None:
        super().__init__()
        self.connection = connection
        self.deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self.deadline.arm()
        return self.connection.recv_into(buffer)


class _BodyInput(io.RawIOBase):
    """End at Content-Length or decoded chunk EOF, never drain trailing traffic."""

    def __init__(self, source: Any, deadline: _ReceiveDeadline, remaining: int | None) -> None:
        super().__init__()
        self.source = source
        self.deadline = deadline
        self.remaining = remaining
        self.done = remaining == 0
        if self.done:
            deadline.complete()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.done:
            return 0
        view = memoryview(buffer)
        if not view:
            return 0
        if self.remaining is not None:
            view = view[: self.remaining]
        # BufferedReader.read may issue many reads with one stale socket timeout.
        # read1 returns after one underlying read; _SocketInput re-arms each one.
        try:
            self.deadline.arm()
            if hasattr(self.source, "read1"):
                data = self.source.read1(len(view))
                size = len(data)
                view[:size] = data
            else:
                # Werkzeug's DechunkedInput uses the deadline-aware header stream.
                size = self.source.readinto(view)
        except TimeoutError:
            self.done = True
            self.deadline.complete()
            raise RequestTimeout() from None
        except OSError:
            self.done = True
            self.deadline.complete()
            raise BadRequest("invalid HTTP request body framing") from None
        if self.remaining is not None:
            self.remaining -= size
            if not size and self.remaining:
                self.done = True
                self.deadline.complete()
                raise ClientDisconnected()
        if not size or self.remaining == 0:
            self.done = True
            self.deadline.complete()
        return size

    def close(self) -> None:
        try:
            self.source.close()
        finally:
            super().close()


class _RequestHandler(WSGIRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.receive_deadline = _ReceiveDeadline(self.connection, self.server.receive_timeout)
        self.rfile = io.BufferedReader(_SocketInput(self.connection, self.receive_deadline))

    def make_environ(self) -> dict[str, Any]:
        environ = super().make_environ()
        remaining = None if environ.get("wsgi.input_terminated") else int(environ.get("CONTENT_LENGTH") or 0)
        if remaining is not None and remaining < 0:
            raise ValueError("negative HTTP Content-Length")
        # Limit both application reads and Werkzeug's post-response drain to the
        # actual body. This prevents trailing data from restarting network reads
        # after inference has outlived the receive deadline.
        self.rfile = io.BufferedReader(_BodyInput(environ["wsgi.input"], self.receive_deadline, remaining))
        environ["wsgi.input"] = self.rfile
        return environ


class _HTTPServer(ThreadedWSGIServer):
    def __init__(
        self,
        app: Any,
        host: str,
        port: int,
        tls: ssl.SSLContext | None,
        *,
        handshake_timeout: float,
        receive_timeout: float,
        max_workers: int,
    ) -> None:
        self.handshake_timeout = handshake_timeout
        self.receive_timeout = receive_timeout
        self._slots = threading.BoundedSemaphore(max_workers)
        # Passing tls here would let SSLSocket.accept synchronously handshake.
        super().__init__(host, port, app, handler=_RequestHandler, ssl_context=None)
        self.ssl_context = tls

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        connection = request
        try:
            if self.ssl_context is not None:
                try:
                    connection = self.ssl_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
                    connection.settimeout(self.handshake_timeout)
                    connection.do_handshake()
                except (OSError, ValueError):
                    # Never try an HTTP error response on an incomplete TLS stream.
                    self.shutdown_request(connection)
                    return
            super().process_request_thread(connection, client_address)
        finally:
            self._slots.release()


def make_http_server(
    app: Any,
    host: str = "127.0.0.1",
    port: int = 8000,
    tls: ssl.SSLContext | None = None,
    *,
    handshake_timeout: float = 3.0,
    receive_timeout: float = 10.0,
    max_workers: int = 8,
) -> ThreadedWSGIServer:
    """Create a bound server; callers own serve_forever/shutdown/server_close.

    TLS handshakes have a separate deadline (default 3s). A fresh absolute
    deadline (default 10s) covers the request line, headers and body together.
    At most eight workers run, including handshake, receive and inference.
    Overload closes the accepted connection before TLS or HTTP processing.
    No debugger, reloader, keep-alive or inference timeout is enabled.
    """
    if tls is not None and not isinstance(tls, ssl.SSLContext):
        raise TypeError("tls must be an SSLContext or None")
    for value in (handshake_timeout, receive_timeout):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("transport deadlines must be positive and finite")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
        raise ValueError("max_workers must be between 1 and 8")
    return _HTTPServer(
        app,
        host,
        port,
        tls,
        handshake_timeout=handshake_timeout,
        receive_timeout=receive_timeout,
        max_workers=max_workers,
    )


def run_http_service(
    app: Any,
    host: str = "127.0.0.1",
    port: int = 8000,
    tls: ssl.SSLContext | None = None,
) -> None:
    """Serve until interrupted, with the factory's bounded transport defaults."""
    with make_http_server(app, host, port, tls) as server:
        server.serve_forever()
