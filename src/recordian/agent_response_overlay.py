"""A small, focus-free desktop card for streaming Agent replies."""
from __future__ import annotations

import tkinter as tk

FINISHED = {'completed', 'failed', 'cancelled', 'interrupted'}
HIDE_AFTER_MS = 20000
MAX_REPLY = 60000


def card_content(task: dict, agent_name: str) -> tuple[str, str, str]:
    status = str(task.get('status', ''))
    activity = str(task.get('activity', '')).strip()
    reply = str(task.get('reply', ''))[:MAX_REPLY]
    if status == 'completed':
        return f'{agent_name} · 已完成', reply or '任务已完成，Hermes 没有返回文字。', '#bfe6a0'
    if status == 'failed':
        return f'{agent_name} · 任务未完成', str(task.get('error') or '请查看任务面板。'), '#ffb5a8'
    if status in {'cancelled', 'interrupted'}:
        return f'{agent_name} · 已停止', str(task.get('error') or '任务已停止。'), '#ffd79b'
    return f'{agent_name} · 正在回复', reply or activity or '正在连接 Hermes…', '#d5eaa1'


def active_monitor_geometry() -> tuple[int, int, int, int] | None:
    """Use compositor logical coordinates; do not switch monitors or focus."""
    try:
        from .wayland_desktop import desktop_query

        monitors = desktop_query('monitors')
        active = desktop_query('activewindow')
        monitor = next((m for m in monitors if m.get('id') == active.get('monitor')), None)
        if monitor is None:
            monitor = next(m for m in monitors if m.get('focused'))
        scale = float(monitor.get('scale') or 1)
        return (int(monitor['x']), int(monitor['y']),
                round(int(monitor['width']) / scale), round(int(monitor['height']) / scale))
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return None


class AgentResponseOverlay:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.task_id = ''
        self.current_status = ''
        self.dismissed_id = ''
        self.last_body = ''
        self.hide_job: str | None = None
        self.window = tk.Toplevel(root, class_='RecordianAgentFeedback')
        self.window.withdraw()
        self.window.title('Recordian Agent Feedback')
        self.window.overrideredirect(True)
        self.window.attributes('-alpha', .92)
        self.window.attributes('-topmost', True)
        self.window.configure(background='#202521', highlightbackground='#8da56d', highlightthickness=1)
        try:
            self.window.wm_attributes('-type', 'notification')
        except tk.TclError:
            pass
        self.window.focusmodel('passive')

        frame = tk.Frame(self.window, background='#202521', padx=18, pady=14)
        frame.pack(fill='both', expand=True)
        self.heading = tk.Label(frame, text='', background='#202521', foreground='#d5eaa1',
                                font=('Noto Sans CJK SC', 15, 'bold'), anchor='w')
        self.heading.pack(fill='x')
        hint = tk.Label(frame, text='点击关闭 · 完整回复可在 Agent 面板查看', background='#202521',
                        foreground='#aeb9a8', font=('Noto Sans CJK SC', 10), anchor='w')
        hint.pack(fill='x', pady=(2, 10))
        body_frame = tk.Frame(frame, background='#202521')
        body_frame.pack(fill='both', expand=True)
        self.body = tk.Text(body_frame, wrap='word', background='#202521', foreground='#f0f2eb',
                            insertbackground='#f0f2eb', borderwidth=0, highlightthickness=0,
                            font=('Noto Sans CJK SC', 12), cursor='hand2', state='disabled',
                            spacing2=4, spacing3=7)
        self.body.pack(side='left', fill='both', expand=True)
        scroll = tk.Scrollbar(body_frame, orient='vertical', command=self.body.yview)
        scroll.pack(side='right', fill='y')
        self.body.configure(yscrollcommand=scroll.set)
        for widget in (self.window, frame, self.heading, hint, body_frame, self.body):
            widget.bind('<Button-1>', self._dismiss)

    def _place(self) -> None:
        monitor = active_monitor_geometry()
        if monitor is None:
            monitor = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        mx, my, mw, mh = monitor
        width = min(700, max(320, mw - 64))
        height = min(410, max(220, mh - 110))
        x = mx + mw - width - 28
        y = my + 48
        self.window.geometry(f'{width}x{height}+{x}+{y}')

    def _cancel_hide(self) -> None:
        if self.hide_job is not None:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None

    def _dismiss(self, event=None) -> None:  # noqa: ANN001
        self.dismissed_id = self.task_id
        self.hide()

    def hide(self) -> None:
        self._cancel_hide()
        self.window.withdraw()

    def show_task(self, task: dict, agent_name: str) -> None:
        ident = str(task.get('id', ''))
        if not ident:
            return
        if ident != self.task_id:
            self.task_id = ident
            self.dismissed_id = ''
            self.last_body = ''
            self._place()
        self.current_status = str(task.get('status', ''))
        if ident == self.dismissed_id:
            return
        self._cancel_hide()
        title, body, color = card_content(task, agent_name)
        self.heading.configure(text=title, foreground=color)
        if body != self.last_body:
            at_bottom = self.body.yview()[1] >= .98
            old_top = self.body.yview()[0]
            self.body.configure(state='normal')
            self.body.delete('1.0', 'end')
            self.body.insert('1.0', body)
            self.body.configure(state='disabled')
            if at_bottom:
                self.body.see('end')
            else:
                self.body.yview_moveto(old_top)
            self.last_body = body
        if not self.window.winfo_viewable():
            self.window.deiconify()
        if self.current_status in FINISHED:
            self.hide_job = self.root.after(HIDE_AFTER_MS, self.hide)

    def close(self) -> None:
        self._cancel_hide()
        self.window.destroy()
