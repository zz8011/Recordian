from __future__ import annotations

import argparse
import logging
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

from recordian.linux_commit import _get_clipboard_text, get_focused_window_id, resolve_committer, send_paste_shortcut
from recordian.linux_notify import Notification, resolve_notifier

from .config import load_agent_config
from .protocol import DEFAULT_REMOTE_PASTE_PORT, MAX_MESSAGE_BYTES, decode_message, encode_message, preview_text

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recordian remote paste agent for Linux desktops")
    parser.add_argument("--port", type=int, default=DEFAULT_REMOTE_PASTE_PORT)
    parser.add_argument("--hostname", default=socket.gethostname())
    parser.add_argument("--config", default="")
    parser.add_argument("--enable-notify", dest="enable_notify", action="store_true", default=True)
    parser.add_argument("--no-notify", dest="enable_notify", action="store_false")
    parser.add_argument("--notify-backend", choices=["none", "auto", "notify-send", "stdout"], default="auto")
    parser.add_argument("--paste-delay-ms", type=int, default=100)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-file", default="")
    parser.add_argument(
        "--commit-backend",
        choices=["auto", "auto-fallback", "wtype", "xdotool", "xdotool-clipboard", "stdout", "none"],
        default="auto",
    )
    return parser


def parse_args() -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="")
    pre_args, _ = pre.parse_known_args()

    parser = build_parser()
    config_path = str(pre_args.config).strip()
    if config_path:
        path = Path(config_path).expanduser()
        if path.exists():
            payload = load_agent_config(path)
            allowed = {action.dest for action in parser._actions if action.dest != "help"}
            defaults = {k: v for k, v in payload.items() if k in allowed}
            if defaults:
                parser.set_defaults(**defaults)
    return parser.parse_args()


class RemotePasteTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[socketserver.BaseRequestHandler], app: RemotePasteAgent) -> None:
        self.app = app
        super().__init__(server_address, handler_class)


class RemotePasteRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        response: dict[str, Any]
        try:
            raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES:
                response = self.server.app.error_response("message_too_large")  # type: ignore[attr-defined]
            else:
                payload = decode_message(raw)
                response = self.server.app.handle_payload(payload)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            response = self.server.app.error_response(str(exc))  # type: ignore[attr-defined]
        self.wfile.write(encode_message(response))
        self.wfile.flush()


class RemotePasteAgent:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.started_at = time.monotonic()
        self.notifier = resolve_notifier("none" if not args.enable_notify else args.notify_backend)
        self._paste_lock = threading.Lock()

    def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        if action == "ping":
            return {"status": "pong", "hostname": self.args.hostname}
        if action == "status":
            return {
                "status": "ok",
                "hostname": self.args.hostname,
                "uptime": int(time.monotonic() - self.started_at),
            }
        if action == "paste_only":
            return self._handle_paste_only(payload)
        if action == "paste":
            return self._handle_paste(payload)
        return self.error_response(f"unsupported_action:{action or 'missing'}")

    def error_response(self, detail: str) -> dict[str, Any]:
        return {"status": "error", "hostname": self.args.hostname, "detail": detail}

    def _handle_paste(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", "")).strip()
        if not text:
            return self.error_response("empty_text")

        with self._paste_lock:
            delay_ms = max(0, int(getattr(self.args, "paste_delay_ms", 100)))
            if delay_ms:
                time.sleep(delay_ms / 1000.0)

            target_window_id = get_focused_window_id()
            committer = resolve_committer(self.args.commit_backend, target_window_id=target_window_id)
            result = committer.commit(text)
            if not result.committed:
                return self.error_response(str(result.detail or "commit_failed"))

            self._notify_success(text)
            logger.info(
                "Remote paste succeeded on %s (wid=%s, backend=%s, text=%s)",
                self.args.hostname,
                target_window_id,
                getattr(committer, "backend_name", self.args.commit_backend),
                preview_text(text),
            )
            detail = str(result.detail or "committed")
            if target_window_id is not None:
                detail = f"{detail};wid:{target_window_id}"
            return {"status": "ok", "hostname": self.args.hostname, "detail": detail}

    def _handle_paste_only(self, payload: dict[str, Any]) -> dict[str, Any]:
        preview = str(payload.get("preview", "")).strip()
        expected_text = str(payload.get("expected_text", ""))
        clipboard_wait_s = max(0.0, float(payload.get("clipboard_wait_s", 0.0) or 0.0))
        with self._paste_lock:
            delay_ms = max(0, int(getattr(self.args, "paste_delay_ms", 100)))
            if delay_ms:
                time.sleep(delay_ms / 1000.0)

            if expected_text:
                deadline = time.monotonic() + clipboard_wait_s
                current_clipboard = _get_clipboard_text()
                while current_clipboard != expected_text and time.monotonic() < deadline:
                    time.sleep(0.05)
                    current_clipboard = _get_clipboard_text()
                if current_clipboard != expected_text:
                    logger.warning(
                        "Remote paste-only clipboard mismatch on %s (expected=%s, actual=%s)",
                        self.args.hostname,
                        preview_text(expected_text),
                        preview_text(current_clipboard) if current_clipboard else "<empty>",
                    )
                    return self.error_response("clipboard_not_synced")

            target_window_id = get_focused_window_id()
            result = send_paste_shortcut(target_window_id=target_window_id)
            if not result.committed:
                return self.error_response(str(result.detail or "paste_only_failed"))

            logger.info(
                "Remote paste-only succeeded on %s (wid=%s, preview=%s)",
                self.args.hostname,
                target_window_id,
                preview or "<empty>",
            )
            detail = str(result.detail or "paste_only")
            if target_window_id is not None:
                detail = f"{detail};wid:{target_window_id}"
            return {"status": "ok", "hostname": self.args.hostname, "detail": detail}

    def _notify_success(self, text: str) -> None:
        body = f"已在 {self.args.hostname} 粘贴: {preview_text(text)}"
        try:
            self.notifier.notify(Notification(title="Recordian 跨电脑粘贴", body=body, urgency="low"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("remote paste notification failed: %s", exc)


def _configure_logging(args: argparse.Namespace) -> None:
    level_name = str(getattr(args, "log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    kwargs: dict[str, Any] = {
        "level": level,
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
    }
    log_file = str(getattr(args, "log_file", "")).strip()
    if log_file:
        kwargs["filename"] = log_file
    logging.basicConfig(**kwargs)


def main() -> None:
    args = parse_args()
    _configure_logging(args)
    app = RemotePasteAgent(args)
    with RemotePasteTCPServer(("0.0.0.0", int(args.port)), RemotePasteRequestHandler, app) as server:
        logger.info("recordian-agent listening on 0.0.0.0:%s as %s", args.port, args.hostname)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            logger.info("recordian-agent interrupted, shutting down")
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
