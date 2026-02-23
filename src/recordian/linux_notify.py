from __future__ import annotations

from dataclasses import dataclass
from shutil import which
import subprocess
import sys


class NotifyError(RuntimeError):
    pass


@dataclass(slots=True)
class Notification:
    title: str
    body: str
    urgency: str = "normal"


class Notifier:
    backend_name = "base"

    def notify(self, notification: Notification) -> None:
        raise NotImplementedError


class NoopNotifier(Notifier):
    backend_name = "none"

    def notify(self, notification: Notification) -> None:
        return None


class StdoutNotifier(Notifier):
    backend_name = "stdout"

    def notify(self, notification: Notification) -> None:
        print(
            f"[notify:{notification.urgency}] {notification.title}: {notification.body}",
            file=sys.stderr,
            flush=True,
        )


class NotifySendNotifier(Notifier):
    backend_name = "notify-send"

    def notify(self, notification: Notification) -> None:
        if not which("notify-send"):
            raise NotifyError("notify-send not found in PATH")
        cmd = [
            "notify-send",
            "-a",
            "Recordian",
            "-u",
            _normalize_urgency(notification.urgency),
            notification.title,
            notification.body,
        ]
        subprocess.run(cmd, check=False)


def resolve_notifier(backend: str) -> Notifier:
    normalized = backend.strip().lower()
    if normalized == "none":
        return NoopNotifier()
    if normalized == "stdout":
        return StdoutNotifier()
    if normalized == "notify-send":
        return NotifySendNotifier()
    if normalized == "auto":
        if which("notify-send"):
            return NotifySendNotifier()
        return StdoutNotifier()
    raise ValueError(f"unsupported notify backend: {backend}")


def _normalize_urgency(level: str) -> str:
    normalized = level.strip().lower()
    if normalized in {"low", "normal", "critical"}:
        return normalized
    return "normal"
