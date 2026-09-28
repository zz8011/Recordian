from __future__ import annotations

import threading

from recordian import agent_feedback


def test_notification_action_opens_panel_without_exposing_token(monkeypatch):
    opened = threading.Event()
    commands = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)

        def communicate(self, timeout=None):
            return 'open\n', ''

    monkeypatch.setattr(agent_feedback, 'which', lambda name: '/usr/bin/notify-send')
    monkeypatch.setattr(agent_feedback.subprocess, 'Popen', FakeProcess)
    agent_feedback.notify_agent_task(
        {'status': 'completed', 'reply': '图片已画好'}, 'Hermes', opened.set,
    )
    assert opened.wait(2)
    assert '图片已画好' in commands[0]
    assert 'open=查看完整回复' in commands[0]
    assert not any('token' in part.lower() for part in commands[0])


def test_long_reply_is_short_in_notification():
    title, body, urgency = agent_feedback.feedback_message(
        {'status': 'completed', 'reply': '画' * 300}, 'Hermes',
    )
    assert title == 'Hermes 已完成'
    assert len(body) == 180 and body.endswith('…')
    assert urgency == 'normal'
