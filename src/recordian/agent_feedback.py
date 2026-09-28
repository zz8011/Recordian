"""Desktop feedback for Agent tasks without requiring the Hermes window."""
from __future__ import annotations

import subprocess
import threading
from shutil import which


def _preview(value: str, limit: int = 180) -> str:
    text = ' '.join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def feedback_message(task: dict, agent_name: str) -> tuple[str, str, str]:
    status = task['status']
    if status == 'running':
        return f'正在交给 {agent_name}', '语音指令已识别，正在提交和执行。完成后会再次提醒。', 'low'
    if status == 'completed':
        reply = _preview(str(task.get('reply') or '任务已完成，Hermes 没有返回文字。'))
        return f'{agent_name} 已完成', reply, 'normal'
    if status == 'failed':
        return f'{agent_name} 任务未完成', _preview(str(task.get('error') or '请查看任务面板。')), 'critical'
    return f'{agent_name} 任务已停止', _preview(str(task.get('error') or '任务已停止。')), 'normal'


def notify_agent_task(task: dict, agent_name: str, open_panel) -> None:
    """Show a short result; the action opens the authenticated local panel.

    notify-send's action waits for a click/dismissal, so keep it off the
    Agent worker and cap its lifetime. No panel token enters the notification.
    """
    if which('notify-send') is None:
        return
    title, body, urgency = feedback_message(task, agent_name)
    command = ['notify-send', '-a', 'Recordian', '-u', urgency, '-t', '15000',
               '-A', 'open=查看完整回复', title, body]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return

    def wait_for_action():
        try:
            try:
                action, _ = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return
            if action.strip() == 'open':
                open_panel()
        except (OSError, subprocess.SubprocessError):
            pass

    threading.Thread(target=wait_for_action, daemon=True, name='recordian-agent-feedback').start()
