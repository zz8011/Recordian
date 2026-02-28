from recordian import linux_commit


def test_set_clipboard_text_prefers_xclip(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def _fake_which(name: str):  # noqa: ANN001
        if name == "xclip":
            return "/usr/bin/xclip"
        return None

    def _fake_run(cmd: list[str], text: str) -> None:
        calls.append((cmd, text))

    monkeypatch.setattr(linux_commit, "which", _fake_which)
    monkeypatch.setattr(linux_commit, "_run_command_with_input", _fake_run)

    linux_commit._set_clipboard_text("你好，world")
    assert calls == [(["xclip", "-selection", "clipboard", "-i"], "你好，world")]



def test_run_command_with_input_no_dead_returncode_check() -> None:
    import inspect
    from recordian import linux_commit
    source = inspect.getsource(linux_commit._run_command_with_input)
    lines = [l for l in source.split("\n") if "returncode != 0" in l]
    assert len(lines) == 0, f"发现死代码: {lines}"


def test_xdotool_clipboard_committer_clears_clipboard_after_timeout(monkeypatch):
    """剪贴板应在指定超时后自动清空"""
    import time
    from recordian.linux_commit import XdotoolClipboardCommitter

    clipboard_calls: list[str] = []

    def _fake_set_clipboard(text: str) -> None:
        clipboard_calls.append(f"set:{text}")

    def _fake_xdotool_key(shortcut: str, *, window_id=None) -> None:
        pass

    monkeypatch.setattr("recordian.linux_commit._set_clipboard_text", _fake_set_clipboard)
    monkeypatch.setattr("recordian.linux_commit._xdotool_key", _fake_xdotool_key)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = XdotoolClipboardCommitter(clipboard_timeout_ms=50)
    committer.commit("测试文本")

    # 等待超时
    time.sleep(0.1)

    # 应该有两次调用：设置内容 + 清空
    assert len(clipboard_calls) == 2
    assert clipboard_calls[0] == "set:测试文本"
    assert clipboard_calls[1] == "set:"


def test_clipboard_timeout_invalid_env_var_uses_default(monkeypatch):
    """无效的环境变量应使用默认值 0（禁用）"""
    import os
    from recordian.linux_commit import resolve_committer

    monkeypatch.setenv("RECORDIAN_CLIPBOARD_TIMEOUT_MS", "invalid")
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = resolve_committer("xdotool-clipboard")
    assert committer.clipboard_timeout_ms == 0


def test_clipboard_timeout_negative_value_uses_default(monkeypatch):
    """负数超时应使用默认值 0"""
    import os
    from recordian.linux_commit import resolve_committer

    monkeypatch.setenv("RECORDIAN_CLIPBOARD_TIMEOUT_MS", "-100")
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = resolve_committer("xdotool-clipboard")
    assert committer.clipboard_timeout_ms == 0


def test_xdotool_clipboard_multiple_commits_cancel_previous_timer(monkeypatch):
    """快速连续调用 commit 应取消之前的定时器"""
    import time
    from recordian.linux_commit import XdotoolClipboardCommitter

    clipboard_calls: list[str] = []

    def _fake_set_clipboard(text: str) -> None:
        clipboard_calls.append(f"set:{text}")

    def _fake_xdotool_key(shortcut: str, *, window_id=None) -> None:
        pass

    monkeypatch.setattr("recordian.linux_commit._set_clipboard_text", _fake_set_clipboard)
    monkeypatch.setattr("recordian.linux_commit._xdotool_key", _fake_xdotool_key)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    # timeout 大于单次 commit 内部的粘贴延时（0.1s），确保第二次 commit 有机会取消第一次定时器
    committer = XdotoolClipboardCommitter(clipboard_timeout_ms=500)
    committer.commit("文本1")
    time.sleep(0.02)
    committer.commit("文本2")

    time.sleep(0.55)

    # 应该只有 3 次调用：set:文本1, set:文本2, set:（最后一次清空）
    assert len(clipboard_calls) == 3
    assert clipboard_calls[0] == "set:文本1"
    assert clipboard_calls[1] == "set:文本2"
    assert clipboard_calls[2] == "set:"


def test_send_hard_enter_xdotool_clipboard(monkeypatch) -> None:
    from recordian.linux_commit import XdotoolClipboardCommitter, send_hard_enter

    calls: list[tuple[str, int | None]] = []

    def _fake_xdotool_key(shortcut: str, *, window_id=None) -> None:
        calls.append((shortcut, window_id))

    monkeypatch.setattr("recordian.linux_commit._send_hard_enter_via_pynput", lambda: False)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._xdotool_key", _fake_xdotool_key)

    committer = XdotoolClipboardCommitter(target_window_id=12345, clipboard_timeout_ms=0)
    result = send_hard_enter(committer)
    assert result.committed is True
    assert calls == [("return", 12345)]


def test_send_hard_enter_unsupported_backend() -> None:
    from recordian.linux_commit import NoopCommitter, send_hard_enter

    result = send_hard_enter(NoopCommitter())
    assert result.committed is False
    assert "unsupported" in result.detail


def test_send_hard_enter_prefers_pynput(monkeypatch) -> None:
    from recordian.linux_commit import XdotoolClipboardCommitter, send_hard_enter

    called = {"xdotool": False}

    def _fake_xdotool_key(shortcut: str, *, window_id=None) -> None:  # noqa: ANN001
        called["xdotool"] = True

    monkeypatch.setattr("recordian.linux_commit._send_hard_enter_via_pynput", lambda: True)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._xdotool_key", _fake_xdotool_key)

    committer = XdotoolClipboardCommitter(target_window_id=12345, clipboard_timeout_ms=0)
    result = send_hard_enter(committer)
    assert result.committed is True
    assert "pynput" in result.detail
    assert called["xdotool"] is False
