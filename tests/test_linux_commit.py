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
    assert slept
    assert slept[0] >= 0.2


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
