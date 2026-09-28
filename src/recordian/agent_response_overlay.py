"""A movable, resizable floating card for streaming Agent replies."""
from __future__ import annotations

import json
import os
import subprocess
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


def active_monitor_geometry() -> tuple[int, int, int, int, float] | None:
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
                round(int(monitor['width']) / scale), round(int(monitor['height']) / scale), scale)
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
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._resize_origin: tuple[int, int, int, int] | None = None
        self._desired_position: tuple[int, int] | None = None
        self._scale = 1.0
        self.window = tk.Toplevel(root, class_='RecordianAgentFeedback')
        self.window.withdraw()
        self.window.title('Recordian Agent Feedback')
        self.window.resizable(True, True)
        self.window.configure(background='#202521', highlightbackground='#8da56d', highlightthickness=1)
        try:
            # Utility windows float under Hyprland, but still receive real
            # pointer events and can be moved/resized by the user.
            self.window.wm_attributes('-type', 'utility')
        except tk.TclError:
            pass
        self.window.focusmodel('passive')

        self.frame = tk.Frame(self.window, background='#202521', padx=18, pady=14)
        frame = self.frame
        frame.pack(fill='both', expand=True)
        self.header = tk.Frame(frame, background='#202521', cursor='fleur')
        self.header.pack(fill='x')
        self.heading = tk.Label(self.header, text='', background='#202521', foreground='#d5eaa1',
                                font=('Noto Sans CJK SC', 15, 'bold'), anchor='w', cursor='fleur')
        self.heading.pack(side='left', fill='x', expand=True)
        self.close_button = tk.Button(self.header, text='×', command=self._dismiss, takefocus=False,
                                      background='#394039', foreground='#f0f2eb', activebackground='#526052',
                                      activeforeground='#ffffff', borderwidth=0, font=('Sans', 15), cursor='hand2')
        self.close_button.pack(side='right', padx=(10, 0))
        self.hint = tk.Label(frame, text='拖动标题移动 · 拖动右下角缩放 · 点击 × 关闭', background='#202521',
                             foreground='#aeb9a8', font=('Noto Sans CJK SC', 10), anchor='w')
        self.hint.pack(fill='x', pady=(2, 10))
        body_frame = tk.Frame(frame, background='#202521')
        body_frame.pack(fill='both', expand=True)
        self.body = tk.Text(body_frame, wrap='word', background='#202521', foreground='#f0f2eb',
                            insertbackground='#f0f2eb', borderwidth=0, highlightthickness=0,
                            font=('Noto Sans CJK SC', 12), cursor='hand2', state='disabled',
                            width=1, height=1, spacing2=4, spacing3=7)
        self.body.pack(side='left', fill='both', expand=True)
        self.scroll = tk.Scrollbar(body_frame, orient='vertical', command=self.body.yview)
        self.scroll.pack(side='right', fill='y')
        self.body.configure(yscrollcommand=self.scroll.set)
        self.footer = tk.Frame(frame, background='#202521')
        self.footer.pack(fill='x', side='bottom', before=body_frame, pady=(8, 0))
        self.grip = tk.Label(self.footer, text='◢', background='#202521', foreground='#aeb9a8',
                             font=('Sans', 15), cursor='bottom_right_corner')
        self.grip.pack(side='right')
        for widget in (self.header, self.heading):
            widget.bind('<ButtonPress-1>', self._begin_drag)
            widget.bind('<B1-Motion>', self._drag)
            widget.bind('<ButtonRelease-1>', self._end_interaction)
        self.grip.bind('<ButtonPress-1>', self._begin_resize)
        self.grip.bind('<B1-Motion>', self._resize)
        self.grip.bind('<ButtonRelease-1>', self._end_interaction)
        for widget in (self.window, frame, self.body, self.scroll):
            widget.bind('<MouseWheel>', self._extend_hide, add='+')
            widget.bind('<Button-4>', self._extend_hide, add='+')
            widget.bind('<Button-5>', self._extend_hide, add='+')

    def _place(self) -> None:
        monitor = active_monitor_geometry()
        if monitor is None:
            monitor = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight(), 1.0)
        mx, my, mw, mh, scale = monitor
        self._scale = scale
        self._scale_widgets(scale)
        width = min(700, max(320, mw - 64))
        height = min(410, max(220, mh - 110))
        x = mx + mw - width - 28
        y = my + 48
        self._desired_position = (x, y)
        self.window.minsize(round(420 * scale), round(250 * scale))
        # Tk's XWayland geometry is in physical pixels. Hyprland's monitor
        # coordinates are logical, so scale the size and local position.
        self.window.geometry(
            f'{round(width * scale)}x{round(height * scale)}+'
            f'{round((x - mx) * scale)}+{round((y - my) * scale)}'
        )

    def _scale_widgets(self, scale: float) -> None:
        """Tk runs in physical XWayland pixels on a scaled display."""
        def px(size: int) -> int:
            return max(1, round(size * scale))

        self.frame.configure(padx=px(18), pady=px(14))
        self.heading.configure(font=('Noto Sans CJK SC', px(15), 'bold'))
        self.close_button.configure(font=('Sans', px(15)), padx=px(9), pady=px(1))
        self.close_button.pack_configure(padx=(px(10), 0))
        self.hint.configure(font=('Noto Sans CJK SC', px(10)))
        self.hint.pack_configure(pady=(px(2), px(10)))
        self.body.configure(font=('Noto Sans CJK SC', px(12)), spacing2=px(4), spacing3=px(7))
        self.scroll.configure(width=px(12))
        self.footer.pack_configure(pady=(px(8), 0))
        self.grip.configure(font=('Sans', px(15)), padx=px(6), pady=px(3))

    def _place_with_compositor(self) -> None:
        if self._desired_position is None or not self.window.winfo_viewable():
            return
        try:
            from .wayland_desktop import desktop_query

            client = next(c for c in desktop_query('clients')
                          if c.get('class') == 'RecordianAgentFeedback' and c.get('pid') == os.getpid())
            x, y = self._desired_position
            selector = json.dumps('address:' + client['address'])
            lua = (
                f'hl.dispatch(hl.dsp.window.move({{x={x},y={y},relative=false,window={selector}}}));'
                f'return hl.dispatch(hl.dsp.window.alter_zorder({{mode="top",window={selector}}}))'
            )
            subprocess.run(['hyprctl', 'eval', lua], capture_output=True, text=True, timeout=.6, check=False)
        except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
            pass

    def _cancel_hide(self) -> None:
        if self.hide_job is not None:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None

    def _dismiss(self, event=None) -> None:  # noqa: ANN001
        self.dismissed_id = self.task_id
        self.hide()

    def _begin_drag(self, event) -> None:  # noqa: ANN001
        self._drag_origin = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y())
        self._cancel_hide()

    def _drag(self, event) -> None:  # noqa: ANN001
        if self._drag_origin is None:
            return
        px, py, wx, wy = self._drag_origin
        x, y = wx + event.x_root - px, wy + event.y_root - py
        self.window.geometry(f'{x:+d}{y:+d}')

    def _begin_resize(self, event) -> None:  # noqa: ANN001
        self._resize_origin = (event.x_root, event.y_root, self.window.winfo_width(), self.window.winfo_height())
        self._cancel_hide()

    def _resize(self, event) -> None:  # noqa: ANN001
        if self._resize_origin is None:
            return
        px, py, width, height = self._resize_origin
        width = max(round(420 * self._scale), width + event.x_root - px)
        height = max(round(250 * self._scale), height + event.y_root - py)
        self.window.geometry(f'{width}x{height}')

    def _end_interaction(self, event=None) -> None:  # noqa: ANN001
        self._drag_origin = None
        self._resize_origin = None
        self._extend_hide()

    def _extend_hide(self, event=None) -> None:  # noqa: ANN001
        if self.current_status in FINISHED and self.window.winfo_viewable():
            self._cancel_hide()
            self.hide_job = self.root.after(HIDE_AFTER_MS, self.hide)

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
            # Tk drops the alpha value set while withdrawn on XWayland.
            self.window.attributes('-alpha', .92)
            self.window.attributes('-topmost', True)
            self.root.after(100, self._place_with_compositor)
        if self.current_status in FINISHED:
            self._extend_hide()

    def close(self) -> None:
        self._cancel_hide()
        self.window.destroy()
