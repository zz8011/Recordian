from __future__ import annotations

from recordian.agent_response_overlay import MAX_REPLY, card_content
from recordian.tray_app import TrayApp


def test_reply_card_keeps_full_streamed_text_and_explains_failures():
    response = '画' * 500
    title, body, _ = card_content({'status': 'running', 'reply': response}, 'Hermes')
    assert title == 'Hermes · 正在回复'
    assert body == response
    title, body, _ = card_content({'status': 'completed', 'reply': response}, 'Hermes')
    assert title == 'Hermes · 已完成'
    assert body == response
    assert len(card_content({'status': 'completed', 'reply': '字' * (MAX_REPLY + 100)}, 'Hermes')[1]) == MAX_REPLY
    assert '会话' in card_content({'status': 'failed', 'error': '会话正在使用'}, 'Hermes')[1]


def test_agent_task_events_go_to_overlay_without_refreshing_tray_menu():
    received = []

    class FakeOverlay:
        def show_task(self, task, name):
            received.append((task, name))

    app = TrayApp.__new__(TrayApp)
    app.agent_overlay = FakeOverlay()
    app._update_tray_menu = lambda: (_ for _ in ()).throw(AssertionError('menu refreshed'))
    task = {'id': 'task-1', 'event_type': 'stream', 'status': 'running', 'reply': '正在生成'}
    app._handle_event({'event': 'agent_task', 'task': task, 'agent_name': 'Hermes'})
    assert received == [(task, 'Hermes')]
