from recordian.linux_notify import Notification, StdoutNotifier, resolve_notifier


def test_resolve_notifier_auto_prefers_notify_send(monkeypatch) -> None:
    monkeypatch.setattr("recordian.linux_notify.which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None)
    notifier = resolve_notifier("auto")
    assert notifier.backend_name == "notify-send"


def test_resolve_notifier_auto_fallback_stdout(monkeypatch) -> None:
    monkeypatch.setattr("recordian.linux_notify.which", lambda cmd: None)
    notifier = resolve_notifier("auto")
    assert isinstance(notifier, StdoutNotifier)


def test_notify_send_invokes_subprocess(monkeypatch) -> None:
    monkeypatch.setattr("recordian.linux_notify.which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None)
    calls: list[list[str]] = []

    def _fake_run(cmd, check):  # noqa: ANN001
        calls.append(cmd)
        return None

    monkeypatch.setattr("recordian.linux_notify.subprocess.run", _fake_run)
    notifier = resolve_notifier("notify-send")
    notifier.notify(Notification(title="Recordian", body="ok", urgency="low"))
    assert calls
    assert calls[0][0] == "notify-send"
