import subprocess
from unittest.mock import Mock

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


def test_get_clipboard_text_prefers_xsel(monkeypatch) -> None:
    class _Result:
        stdout = "剪贴板内容"

    monkeypatch.setattr(
        linux_commit,
        "which",
        lambda name: "/usr/bin/xsel" if name == "xsel" else None,
    )
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Result())

    assert linux_commit._get_clipboard_text() == "剪贴板内容"



def test_run_command_with_input_no_dead_returncode_check() -> None:
    import inspect

    from recordian import linux_commit
    source = inspect.getsource(linux_commit._run_command_with_input)
    lines = [line for line in source.split("\n") if "returncode != 0" in line]
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
    monkeypatch.setattr("recordian.linux_commit._start_transient_clipboard_owner", lambda text: None)
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


def test_xdotool_clipboard_committer_waits_for_clipboard_settle(monkeypatch):
    from recordian.linux_commit import XdotoolClipboardCommitter

    order: list[str] = []
    slept: list[float] = []

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._start_transient_clipboard_owner", lambda text: object())
    monkeypatch.setattr("recordian.linux_commit._stop_transient_clipboard_owner", lambda proc: order.append("owner:stop"))
    monkeypatch.setattr("recordian.linux_commit._xdotool_key", lambda shortcut, *, window_id=None: order.append(f"paste:{shortcut}"))
    monkeypatch.setattr("time.sleep", lambda seconds: slept.append(seconds))

    committer = XdotoolClipboardCommitter(clipboard_timeout_ms=0)
    committer.commit("测试文本")

    assert order == ["paste:ctrl+v", "owner:stop"]
    assert len(slept) >= 2
    assert slept[0] >= 0.2
    assert slept[1] >= 0.15


def test_xdotool_clipboard_committer_falls_back_when_no_transient_owner(monkeypatch):
    from recordian.linux_commit import XdotoolClipboardCommitter

    order: list[str] = []

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._start_transient_clipboard_owner", lambda text: None)
    monkeypatch.setattr("recordian.linux_commit._set_clipboard_text", lambda text: order.append(f"clipboard:{text}"))
    monkeypatch.setattr("recordian.linux_commit._xdotool_key", lambda shortcut, *, window_id=None: order.append(f"paste:{shortcut}"))
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    committer = XdotoolClipboardCommitter(clipboard_timeout_ms=0)
    committer.commit("测试文本")

    assert order == ["clipboard:测试文本", "paste:ctrl+v"]


def test_clipboard_timeout_invalid_env_var_uses_default(monkeypatch):
    """无效的环境变量应使用默认值 0（禁用）"""
    from recordian.linux_commit import resolve_committer

    monkeypatch.setenv("RECORDIAN_CLIPBOARD_TIMEOUT_MS", "invalid")
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = resolve_committer("xdotool-clipboard")
    assert committer.clipboard_timeout_ms == 0


def test_clipboard_timeout_negative_value_uses_default(monkeypatch):
    """负数超时应使用默认值 0"""
    from recordian.linux_commit import resolve_committer

    monkeypatch.setenv("RECORDIAN_CLIPBOARD_TIMEOUT_MS", "-100")
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = resolve_committer("xdotool-clipboard")
    assert committer.clipboard_timeout_ms == 0


def test_resolve_streaming_committer_never_converts_clipboard_to_key_stream(monkeypatch):
    """Clipboard backends must stay themselves for streaming.

    Converting xdotool-clipboard into per-keystroke xdotool typing would
    inject a dangerous synthetic key stream (typing + backspaces). The safe
    contract is preview-only streaming with one final commit.
    """
    from recordian.linux_commit import (
        XdotoolClipboardCommitter,
        XDoToolCommitter,
        resolve_streaming_committer,
    )

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = XdotoolClipboardCommitter(target_window_id=42)
    streaming_committer = resolve_streaming_committer(committer)

    assert streaming_committer is committer
    assert not isinstance(streaming_committer, XDoToolCommitter)
    # Counter-proof: no streaming keystroke committer is ever produced from
    # a clipboard backend.
    for window_id in (42, 7):
        committer = XdotoolClipboardCommitter(target_window_id=window_id)
        assert not isinstance(resolve_streaming_committer(committer), XDoToolCommitter)


def test_resolve_streaming_committer_keeps_clipboard_for_electron(monkeypatch):
    from recordian.linux_commit import XdotoolClipboardCommitter, resolve_streaming_committer

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._is_electron_window", lambda wid: wid == 42)

    committer = XdotoolClipboardCommitter(target_window_id=42)
    streaming_committer = resolve_streaming_committer(committer)

    assert streaming_committer is committer


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
    monkeypatch.setattr("recordian.linux_commit._start_transient_clipboard_owner", lambda text: None)
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

    calls: list[int | None] = []

    def _fake_hard_return(*, window_id=None) -> None:
        calls.append(window_id)

    monkeypatch.setattr("recordian.linux_commit._send_hard_enter_via_pynput", lambda: False)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._xdotool_hard_return", _fake_hard_return)
    monkeypatch.setattr("recordian.linux_commit.get_focused_window_id", lambda: 12345)

    committer = XdotoolClipboardCommitter(target_window_id=12345, clipboard_timeout_ms=0)
    result = send_hard_enter(committer)
    assert result.committed is True
    assert calls == [12345]
    assert "focus_before:12345" in result.detail


def test_send_hard_enter_unsupported_backend() -> None:
    from recordian.linux_commit import NoopCommitter, send_hard_enter

    result = send_hard_enter(NoopCommitter())
    assert result.committed is False
    assert "unsupported" in result.detail


def test_send_hard_enter_prefers_backend_specific_path_over_pynput(monkeypatch) -> None:
    from recordian.linux_commit import XdotoolClipboardCommitter, send_hard_enter

    called = {"xdotool": False}

    def _fake_hard_return(*, window_id=None) -> None:  # noqa: ANN001
        called["xdotool"] = True

    monkeypatch.setattr("recordian.linux_commit._send_hard_enter_via_pynput", lambda: True)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._xdotool_hard_return", _fake_hard_return)
    monkeypatch.setattr("recordian.linux_commit.get_focused_window_id", lambda: 12345)

    committer = XdotoolClipboardCommitter(target_window_id=12345, clipboard_timeout_ms=0)
    result = send_hard_enter(committer)
    assert result.committed is True
    assert "pynput" not in result.detail
    assert called["xdotool"] is True


def test_send_hard_enter_resolves_wrapped_fallback_committer(monkeypatch) -> None:
    from recordian.linux_commit import (
        CommitterWithFallback,
        StdoutCommitter,
        XdotoolClipboardCommitter,
        send_hard_enter,
    )

    calls: list[int | None] = []

    def _fake_hard_return(*, window_id=None) -> None:
        calls.append(window_id)

    monkeypatch.setattr("recordian.linux_commit._send_hard_enter_via_pynput", lambda: False)
    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x if x == "xdotool" else None)
    monkeypatch.setattr("recordian.linux_commit._xdotool_hard_return", _fake_hard_return)
    monkeypatch.setattr("recordian.linux_commit.get_focused_window_id", lambda: 54321)

    committer = CommitterWithFallback(
        committers=[
            (XdotoolClipboardCommitter(target_window_id=54321, clipboard_timeout_ms=0), "xdotool-clipboard"),
            (StdoutCommitter(), "stdout"),
        ],
        notify_on_fallback=False,
    )
    result = send_hard_enter(committer)

    assert result.committed is True
    assert result.backend == "xdotool-clipboard-fallback"
    assert calls == [54321]


def test_xdotool_hard_return_uses_press_release(monkeypatch) -> None:
    from recordian.linux_commit import _xdotool_hard_return

    calls: list[list[str]] = []

    monkeypatch.setattr("recordian.linux_commit._send_hard_enter_via_xtest", lambda *, window_id=None: False)
    monkeypatch.setattr("recordian.linux_commit.get_focused_window_id", lambda: 2468)
    monkeypatch.setattr("recordian.linux_commit.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run",
        lambda cmd, check=True: calls.append(cmd),
    )

    _xdotool_hard_return(window_id=2468)

    assert calls == [
        ["xdotool", "keydown", "--clearmodifiers", "Return"],
        ["xdotool", "keyup", "--clearmodifiers", "Return"],
    ]


def test_xdotool_hard_return_prefers_xtest_when_available(monkeypatch) -> None:
    from recordian.linux_commit import _xdotool_hard_return

    calls: list[int | None] = []

    monkeypatch.setattr(
        "recordian.linux_commit._send_hard_enter_via_xtest",
        lambda *, window_id=None: calls.append(window_id) or True,
    )
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run",
        lambda cmd, check=True: (_ for _ in ()).throw(AssertionError("subprocess should not be called")),
    )

    _xdotool_hard_return(window_id=2468)

    assert calls == [2468]


def test_xdotool_key_skips_redundant_refocus_when_target_already_active(monkeypatch) -> None:
    from recordian.linux_commit import _xdotool_key

    focus_calls: list[int] = []
    run_calls: list[list[str]] = []

    monkeypatch.setattr("recordian.linux_commit.get_focused_window_id", lambda: 2468)
    monkeypatch.setattr("recordian.linux_commit._xdotool_focus_window", lambda window_id: focus_calls.append(window_id))
    monkeypatch.setattr("recordian.linux_commit.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run",
        lambda cmd, check=True: run_calls.append(cmd),
    )

    _xdotool_key("ctrl+v", window_id=2468)

    assert focus_calls == []
    assert run_calls == [["xdotool", "key", "--clearmodifiers", "ctrl+v"]]


def test_xdotool_key_refocuses_when_target_window_changed(monkeypatch) -> None:
    from recordian.linux_commit import _xdotool_key

    focus_calls: list[int] = []
    run_calls: list[list[str]] = []
    sleep_calls: list[float] = []

    monkeypatch.setattr("recordian.linux_commit.get_focused_window_id", lambda: 1357)
    monkeypatch.setattr("recordian.linux_commit._xdotool_focus_window", lambda window_id: focus_calls.append(window_id))
    monkeypatch.setattr("recordian.linux_commit.time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run",
        lambda cmd, check=True: run_calls.append(cmd),
    )

    _xdotool_key("ctrl+v", window_id=2468)

    assert focus_calls == [2468]
    assert sleep_calls == [0.15]
    assert run_calls == [["xdotool", "key", "--clearmodifiers", "ctrl+v"]]


def test_paste_to_enter_delay_seconds_only_for_paste_style_commits() -> None:
    from recordian.linux_commit import CommitResult, paste_to_enter_delay_seconds

    assert paste_to_enter_delay_seconds(CommitResult(backend="xdotool-clipboard", committed=True, detail="paste:ctrl+v")) > 0.0
    assert paste_to_enter_delay_seconds(CommitResult(backend="xdotool", committed=True, detail="typed")) == 0.0


# ============================================================================
# Electron Detection Tests
# ============================================================================

def test_is_electron_window_detects_wechat(monkeypatch):
    """测试检测微信 Electron 应用"""
    from recordian.linux_commit import _is_electron_window

    def _fake_run(cmd, **kwargs):
        result = Mock()
        result.stdout = "WM_CLASS(STRING) = \"wechat\", \"WeChatAppEx\"\n_NET_WM_NAME(UTF8_STRING) = \"微信\""
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    # 清空缓存
    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_electron_window(12345) is True


def test_is_electron_window_detects_vscode(monkeypatch):
    """测试检测 VS Code Electron 应用"""
    from recordian.linux_commit import _is_electron_window

    def _fake_run(cmd, **kwargs):
        result = Mock()
        result.stdout = "WM_CLASS(STRING) = \"code\", \"Code\"\n_NET_WM_NAME(UTF8_STRING) = \"Visual Studio Code\""
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_electron_window(12345) is True


def test_is_electron_window_rejects_firefox(monkeypatch):
    """测试非 Electron 应用返回 False"""
    from recordian.linux_commit import _is_electron_window

    def _fake_run(cmd, **kwargs):
        result = Mock()
        result.stdout = "WM_CLASS(STRING) = \"Navigator\", \"Firefox\"\n_NET_WM_NAME(UTF8_STRING) = \"Mozilla Firefox\""
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_electron_window(12345) is False


def test_is_electron_window_caches_result(monkeypatch):
    """测试检测结果被缓存"""
    from recordian.linux_commit import _is_electron_window

    call_count = {"count": 0}

    def _fake_run(cmd, **kwargs):
        call_count["count"] += 1
        result = Mock()
        result.stdout = "WM_CLASS(STRING) = \"wechat\", \"WeChatAppEx\""
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    # 第一次调用
    assert _is_electron_window(12345) is True
    assert call_count["count"] == 1

    # 第二次调用应该使用缓存
    assert _is_electron_window(12345) is True
    assert call_count["count"] == 1


def test_is_electron_window_handles_xprop_failure(monkeypatch):
    """测试 xprop 失败时返回 False"""
    from recordian.linux_commit import _is_electron_window

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 2.0)

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_electron_window(12345) is False


def test_is_electron_window_wayland_returns_false(monkeypatch):
    """测试 Wayland 环境返回 False"""

    from recordian.linux_commit import _is_electron_window

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_electron_window(12345) is False


def test_is_terminal_window_detects_gnome_terminal(monkeypatch):
    """测试检测 GNOME Terminal"""
    from recordian.linux_commit import _is_terminal_window

    def _fake_run(cmd, **kwargs):
        result = Mock()
        result.stdout = "WM_CLASS(STRING) = \"gnome-terminal-server\", \"Gnome-terminal\""
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_terminal_window(12345) is True


def test_is_terminal_window_detects_ghostty(monkeypatch):
    """测试检测 Ghostty"""
    from recordian.linux_commit import _is_terminal_window

    def _fake_run(cmd, **kwargs):
        result = Mock()
        result.stdout = 'WM_CLASS(STRING) = "ghostty", "com.mitchellh.ghostty"'
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_terminal_window(12345) is True


def test_is_terminal_window_rejects_browser(monkeypatch):
    """测试非终端应用返回 False"""
    from recordian.linux_commit import _is_terminal_window

    def _fake_run(cmd, **kwargs):
        result = Mock()
        result.stdout = "WM_CLASS(STRING) = \"Navigator\", \"Firefox\""
        result.returncode = 0
        return result

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("subprocess.run", _fake_run)

    linux_commit._WINDOW_DETECTION_CACHE.clear()

    assert _is_terminal_window(12345) is False


def test_send_paste_shortcut_uses_ctrl_shift_v_for_ghostty(monkeypatch):
    from recordian.linux_commit import send_paste_shortcut

    calls: list[tuple[str, int | None]] = []

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._is_terminal_window", lambda wid: wid == 12345)
    monkeypatch.setattr(
        "recordian.linux_commit._xdotool_key",
        lambda shortcut, *, window_id=None: calls.append((shortcut, window_id)),
    )

    result = send_paste_shortcut(target_window_id=12345)

    assert result.committed is True
    assert result.detail == "paste_only:ctrl+shift+v wid:12345"
    assert calls == [("ctrl+shift+v", 12345)]


# ============================================================================
# Committer Routing Tests
# ============================================================================

def test_resolve_committer_auto_with_electron_window(monkeypatch):
    """测试 auto 模式检测到 Electron 窗口时使用 xdotool-clipboard"""
    from recordian.linux_commit import resolve_committer

    def _fake_is_electron(wid):
        return True

    def _fake_is_terminal(wid):
        return False

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr("recordian.linux_commit._fcitx_channel_available", lambda: False)
    monkeypatch.setattr("recordian.linux_commit._is_electron_window", _fake_is_electron)
    monkeypatch.setattr("recordian.linux_commit._is_terminal_window", _fake_is_terminal)

    committer = resolve_committer("auto", target_window_id=12345)
    assert committer.backend_name == "xdotool-clipboard"
    assert committer.target_window_id == 12345


def test_resolve_committer_auto_fallback_creates_fallback_chain(monkeypatch):
    """测试 auto-fallback 模式创建降级链"""
    from recordian.linux_commit import CommitterWithFallback, resolve_committer

    monkeypatch.setattr("recordian.linux_commit.which", lambda x: "/usr/bin/" + x)

    committer = resolve_committer("auto-fallback", target_window_id=12345)
    assert isinstance(committer, CommitterWithFallback)
    assert len(committer.committers) >= 2


# ============================================================================
# Fallback Mechanism Tests
# ============================================================================

def test_committer_with_fallback_succeeds_on_first_try(monkeypatch):
    """测试主方式成功时不触发降级"""
    from recordian.linux_commit import CommitterWithFallback, StdoutCommitter

    committer = CommitterWithFallback(
        committers=[
            (StdoutCommitter(), "stdout"),
        ],
        notify_on_fallback=False,
    )

    result = committer.commit("测试")
    assert result.committed is False  # stdout 不实际提交
    assert "fallback" not in result.detail


def test_committer_with_fallback_falls_back_on_failure(monkeypatch):
    """测试主方式失败时自动降级"""
    from recordian.exceptions import CommitError
    from recordian.linux_commit import (
        CommitResult,
        CommitterWithFallback,
        StdoutCommitter,
        TextCommitter,
    )

    class FailingCommitter(TextCommitter):
        backend_name = "failing"

        def commit(self, text: str) -> CommitResult:
            raise CommitError("Simulated failure")

    committer = CommitterWithFallback(
        committers=[
            (FailingCommitter(), "failing-backend"),
            (StdoutCommitter(), "stdout-fallback"),
        ],
        notify_on_fallback=False,
    )

    result = committer.commit("测试")
    assert "fallback" in result.detail
    assert "2/2" in result.detail  # 第2个方式成功


def test_committer_with_fallback_raises_on_all_failures(monkeypatch):
    """测试所有方式失败时抛出异常"""
    from recordian.exceptions import CommitError
    from recordian.linux_commit import (
        CommitResult,
        CommitterWithFallback,
        TextCommitter,
    )

    class FailingCommitter(TextCommitter):
        backend_name = "failing"

        def commit(self, text: str) -> CommitResult:
            raise CommitError("Simulated failure")

    committer = CommitterWithFallback(
        committers=[
            (FailingCommitter(), "failing-1"),
            (FailingCommitter(), "failing-2"),
        ],
        notify_on_fallback=False,
    )

    try:
        committer.commit("测试")
        raise AssertionError("应该抛出 CommitError")
    except CommitError as e:
        assert "All 2 committers failed" in str(e)


def test_committer_with_fallback_requires_at_least_one_committer():
    """测试空 committers 列表抛出异常"""
    from recordian.linux_commit import CommitterWithFallback

    try:
        CommitterWithFallback(committers=[])
        raise AssertionError("应该抛出 ValueError")
    except ValueError as e:
        assert "at least one committer" in str(e)


def test_fcitx_committer_sends_commit_text(monkeypatch):
    from recordian.linux_commit import FcitxCommitter

    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = 's "gtk3 gedit"'
            stderr = ""
        return Result()

    monkeypatch.setattr("recordian.linux_commit.which", lambda name: "/usr/bin/" + name if name == "busctl" else None)
    monkeypatch.setattr("recordian.linux_commit.subprocess.run", _run)

    result = FcitxCommitter().commit("输入法")

    assert result.committed is True
    assert result.backend == "fcitx"
    assert calls[-1][-1] == "输入法"
    assert "CommitText" in calls[-1]


def test_auto_prefers_fcitx_when_channel_is_up(monkeypatch):
    from recordian.linux_commit import CommitterWithFallback, resolve_committer

    monkeypatch.setattr("recordian.linux_commit.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("recordian.linux_commit._fcitx_channel_available", lambda: True)

    committer = resolve_committer("auto", target_window_id=7)

    assert isinstance(committer, CommitterWithFallback)
    assert committer.committers[0][0].backend_name == "fcitx"
    assert committer.committers[1][0].backend_name == "xdotool-clipboard"


def test_streaming_committer_keeps_fcitx_channel(monkeypatch):
    from recordian.linux_commit import FcitxCommitter, resolve_committer, resolve_streaming_committer

    monkeypatch.setattr("recordian.linux_commit.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("recordian.linux_commit._fcitx_channel_available", lambda: True)

    streaming = resolve_streaming_committer(resolve_committer("auto", target_window_id=9))

    assert isinstance(streaming, FcitxCommitter)
    assert streaming.streaming is True
    assert streaming.target_window_id == 9


# ---------------------------------------------------------------------------
# busctl reply parsing + composition session edge cases (round 2)
# ---------------------------------------------------------------------------

def test_parse_busctl_string_real_formats():
    """Decode actual `busctl call` stdout shapes (typed + JSON + unicode)."""
    from recordian.linux_commit import _parse_busctl_string

    # Plain typed reply as printed by busctl 255:
    assert _parse_busctl_string('s "ok"\n') == "ok"
    assert _parse_busctl_string('s "committed gtk3 gedit"') == "committed gtk3 gedit"
    assert _parse_busctl_string(
        's "0123abcd preedit=1 frontend=gtk3 program=gtk-demo"'
    ) == "0123abcd preedit=1 frontend=gtk3 program=gtk3-demo".replace(
        "gtk3-demo", "gtk-demo"
    )
    # JSON mode (busctl --json=short):
    assert _parse_busctl_string(
        '{"type":"s","data":"committed wayland WeChat"}'
    ) == "committed wayland WeChat"
    # C-style escapes inside the quoted value:
    assert _parse_busctl_string(r's "said \"hi\""') == 'said "hi"'
    assert _parse_busctl_string(r's "a\nb"') == "a\nb"
    assert _parse_busctl_string('s "\\u4f60\\u597d"') == "你好"
    # Full unicode stays intact when busctl prints it raw:
    assert _parse_busctl_string('s "🎙️录音 📝草稿"') == "🎙️录音 📝草稿"
    # Legacy stub shapes (no signature letter) keep working:
    assert _parse_busctl_string('"committed gtk3 gedit"') == "committed gtk3 gedit"
    assert _parse_busctl_string("committed wayland WeChat") == "committed wayland WeChat"
    assert _parse_busctl_string("") == ""
    # A value that merely starts with 1-3 letters is not mangled:
    assert _parse_busctl_string("ok") == "ok"
    assert _parse_busctl_string("tok1 preedit=1 frontend=x") == "tok1 preedit=1 frontend=x"


def _method_of(cmd) -> str:
    """Return the DBus method name from a busctl call argv list."""
    iface = "org.fcitx.Fcitx.Recordian1"
    return cmd[cmd.index(iface) + 1]


def _busctl_run_factory(replies: list[str], calls: list):
    from types import SimpleNamespace

    def _run(cmd, **kwargs):
        calls.append(cmd)
        raw = replies.pop(0) if replies else 's "ok"'
        # Mirror a real subprocess result: parsing happens in the caller,
        # so hand out the raw busctl stdout line.
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    return _run


def test_begin_composition_parses_busctl_typed_descriptor(monkeypatch):
    """BeginSession token must survive the real busctl 's "..."' wrapper."""
    from recordian.linux_commit import FcitxCommitter

    calls: list = []
    replies = ['s "9f0e1d2c3b4a5f6e preedit=1 frontend=gtk3 program=gtk3-demo-app"']
    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run", _busctl_run_factory(replies, calls)
    )

    session = FcitxCommitter().begin_composition("")
    assert session.token == "9f0e1d2c3b4a5f6e"
    assert session.preedit_capable is True
    assert session.active


def test_session_commit_busctl_reply_marks_committed(monkeypatch):
    """session.commit succeeds when busctl replies 's "committed ..."'."""
    from recordian.linux_commit import FcitxCommitter

    calls: list = []
    replies = [
        's "9f0e preedit=1 frontend=gtk3 program=p"',   # BeginSession
        's "updated"',                                  # UpdatePreedit
        's "committed gtk3 p"',                         # CommitSession
    ]
    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run", _busctl_run_factory(replies, calls)
    )

    session = FcitxCommitter().begin_composition("")
    assert session.update_preedit("你好").committed
    result = session.commit("你好世界")
    assert result.committed is True
    assert result.detail == "committed gtk3 p"


def test_session_commit_empty_text_cleared_concludes_session(monkeypatch):
    from recordian.linux_commit import FcitxCommitter

    calls: list = []
    replies = [
        's "9f0e preedit=1 frontend=gtk3 program=p"',
        's "cleared"',
    ]
    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr(
        "recordian.linux_commit.subprocess.run", _busctl_run_factory(replies, calls)
    )

    session = FcitxCommitter().begin_composition("")
    result = session.commit("")
    assert result.committed is True
    assert result.detail == "cleared"
    assert not session.active


def test_session_commit_transport_failure_cancels_preedit(monkeypatch):
    """Non-stale commit failure must not leave our preedit behind."""
    from recordian.linux_commit import FcitxCommitter

    calls: list = []
    replies = ['s "9f0e preedit=1 frontend=gtk3 program=p"']

    from types import SimpleNamespace

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if _method_of(cmd) == "CommitSession":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "Failed to call method: unexpected transport error "
                    "(connection reset by peer)"
                ),
            )
        raw = replies.pop(0) if replies else 's "ok"'
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr("recordian.linux_commit.subprocess.run", _run)

    session = FcitxCommitter().begin_composition("")
    result = session.commit("最终")
    assert result.committed is False
    assert "commit_failed" in result.detail
    assert "preedit_cancelled" in result.detail
    methods = [_method_of(c) for c in calls]
    assert methods.count("CancelSession") == 1
    # Session is closed: no second commit attempt is possible.
    assert not session.active


def test_begin_composition_surfaces_existing_preedit_error(monkeypatch):
    """Addon refusal (user preedit active) must raise, not fake a session."""
    from recordian.exceptions import CommitError
    from recordian.linux_commit import FcitxCommitter

    def _run(cmd, **kwargs):
        class Fail:
            returncode = 1
            stdout = ""
            stderr = (
                "Failed to call method: Reply contains error: "
                "org.fcitx.Fcitx.Recordian.Error.ExistingPreedit: "
                "input context already has a non-empty preedit"
            )
        return Fail()

    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr("recordian.linux_commit.subprocess.run", _run)

    try:
        FcitxCommitter().begin_composition("")
        raised = False
    except CommitError as exc:
        raised = "ExistingPreedit" in str(exc)
    assert raised


def test_session_update_reply_loss_cancels_server_session(monkeypatch):
    """P1 fix: UpdatePreedit applied but its reply was lost (busctl timeout).

    The write may be showing in the focused field, so closing the session
    must send ONE best-effort CancelSession for the original token — the
    session is never reopened and the commit is never retried through any
    path."""
    from types import SimpleNamespace

    from recordian.linux_commit import FcitxCommitter

    calls: list = []
    replies = ['s "9f0e preedit=1 frontend=gtk3 program=p"', 's "updated"']

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "UpdatePreedit":
            # The addon applies the write FIRST (argv carries the text); only
            # the reply dies once the scripted replies run out.
            if not replies:
                raise TimeoutError("busctl timed out")
            return SimpleNamespace(returncode=0, stdout=replies.pop(0), stderr="")
        raw = replies.pop(0) if replies else 's "cancelled"'
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr("recordian.linux_commit.subprocess.run", _run)

    session = FcitxCommitter().begin_composition("")
    assert session.update_preedit("草稿").committed
    lost = session.update_preedit("今天天气不错")
    assert lost.committed is False
    assert lost.outcome == "stale"
    assert not session.active
    methods = [_method_of(c) for c in calls]
    assert methods == ["BeginSession", "UpdatePreedit", "UpdatePreedit", "CancelSession"]
    # CancelSession went out for the ORIGINAL token; the server dropped the
    # session (and its preedit) — no residue until the 120s TTL.
    assert methods.count("CancelSession") == 1
    assert calls[-1][calls[-1].index("s") + 1] == session.token
    # Fail-closed forever after: cancel is a local no-op, commit refused.
    assert session.cancel().detail == "cancel_noop:closed"
    assert session.commit("最终").outcome == "stale"
    assert "CommitSession" not in methods


def test_session_update_failure_keeps_uncertain_diagnostics_when_cleanup_fails(monkeypatch):
    """When even the best-effort CancelSession fails, the diagnostics must
    keep the uncertainty (preedit_may_linger) instead of promising rollback
    — not every toolkit honors the cancel."""
    from types import SimpleNamespace

    from recordian.linux_commit import FcitxCommitter

    calls: list = []
    replies = ['s "9f0e preedit=1 frontend=gtk3 program=p"']

    def _run(cmd, **kwargs):
        calls.append(cmd)
        method = _method_of(cmd)
        if method == "UpdatePreedit":
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Call failed: unknown session"
            )
        if method == "CancelSession":
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Call failed: connection closed"
            )
        raw = replies.pop(0) if replies else 's "ok"'
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(
        "recordian.linux_commit.which",
        lambda name: "/usr/bin/" + name if name == "busctl" else None,
    )
    monkeypatch.setattr("recordian.linux_commit.subprocess.run", _run)

    session = FcitxCommitter().begin_composition("")
    result = session.update_preedit("草稿")
    assert result.outcome == "stale"
    assert not result.committed
    # busctl strips the error name -> fail-closed reason, and the failed
    # cleanup is reported as uncertain residue, never a rollback promise.
    assert "preedit_stale:session_invalidated" in result.detail
    assert "preedit_may_linger" in result.detail
    assert "preedit_cancelled" not in result.detail
    assert not session.active
    methods = [_method_of(c) for c in calls]
    assert methods == ["BeginSession", "UpdatePreedit", "CancelSession"]
