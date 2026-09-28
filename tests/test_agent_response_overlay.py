from __future__ import annotations

from types import SimpleNamespace

from recordian.agent_response_overlay import MAX_REPLY, AgentResponseOverlay, card_content
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


def test_card_drag_resize_and_close_do_not_require_focus():
    geometry = []
    withdrawn = []

    class FakeWindow:
        def winfo_x(self):
            return 100

        def winfo_y(self):
            return 50

        def winfo_width(self):
            return 700

        def winfo_height(self):
            return 410

        def geometry(self, value):
            geometry.append(value)

        def withdraw(self):
            withdrawn.append(True)

    card = AgentResponseOverlay.__new__(AgentResponseOverlay)
    card.window = FakeWindow()
    card.root = SimpleNamespace(after_cancel=lambda _: None)
    card.hide_job = None
    card._scale = 1.0
    card._drag_origin = None
    card._resize_origin = None
    card.task_id = 'one'
    card.current_status = 'running'
    card.dismissed_id = ''
    card._begin_drag(SimpleNamespace(x_root=10, y_root=20))
    card._drag(SimpleNamespace(x_root=40, y_root=30))
    assert geometry[-1] == '+130+60'
    card._end_interaction()
    card._begin_resize(SimpleNamespace(x_root=10, y_root=20))
    card._resize(SimpleNamespace(x_root=-400, y_root=-400))
    assert geometry[-1] == '420x250'
    card._dismiss()
    assert withdrawn == [True]
    assert card.dismissed_id == 'one'
