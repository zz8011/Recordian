import subprocess

import pytest

from recordian import wayland_desktop as desktop
from recordian.exceptions import CommitError
from recordian.linux_commit import FcitxCommitter, NoopCommitter, resolve_streaming_committer


def test_codex_compatibility_selected_before_fcitx_session(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test")
    active = {"class": "chatgpt", "xwayland": False, "address": "0x123"}
    monkeypatch.setattr(desktop, "desktop_query", lambda _: active)
    original = FcitxCommitter()
    selected = desktop.select_desktop_committer(original)
    assert isinstance(selected, desktop.WaylandClipboardCommitter)
    assert selected.address == "0x123"
    active["class"] = "gtk-editor"
    assert desktop.select_desktop_committer(original) is original
    assert original.target_window_address == "0x123"
    assert original.target_xwayland is False
    streaming = resolve_streaming_committer(original)
    assert streaming.target_window_address == "0x123"
    assert streaming.target_xwayland is False
    noop = NoopCommitter()
    assert desktop.select_desktop_committer(noop) is noop


@pytest.mark.parametrize("cmdline", [
    b"ChatGPT\0--enable-wayland-ime\0",
    b"/usr/lib/chatgpt/ChatGPT --ozone-platform=wayland --enable-wayland-ime --wayland-text-input-version=3\0",
])
def test_codex_with_running_ime_keeps_streaming_and_correction(monkeypatch, cmdline):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test")
    monkeypatch.setattr(desktop, "desktop_query", lambda _: {
        "class": "chatgpt", "xwayland": False, "address": "target", "pid": 123,
    })
    monkeypatch.setattr(desktop.Path, "read_bytes", lambda _: cmdline)
    original = FcitxCommitter()
    assert desktop.select_desktop_committer(original) is original


@pytest.mark.parametrize("cmdline", [
    b"ChatGPT\0--ozone-platform=wayland\0",
    b"ChatGPT\0--enable-wayland-ime-disabled\0",
])
def test_saved_or_similar_flags_do_not_enable_native_route(monkeypatch, cmdline):
    monkeypatch.setattr(desktop.Path, "read_bytes", lambda _: cmdline)
    assert not desktop._wayland_ime_enabled(123)


def test_exited_process_keeps_compatibility(monkeypatch):
    def missing(_):
        raise FileNotFoundError
    monkeypatch.setattr(desktop.Path, "read_bytes", missing)
    assert not desktop._wayland_ime_enabled(123)


@pytest.mark.parametrize("addresses,expected_copies", [(["other"], 0), (["target", "other"], 1)])
def test_focus_change_never_sends_paste(monkeypatch, addresses, expected_copies):
    reads = iter(addresses)
    monkeypatch.setattr(desktop, "desktop_query", lambda _: {"address": next(reads)})
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    calls = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(CommitError):
        desktop.WaylandClipboardCommitter("target").commit("不能写到其他窗口")
    assert len(calls) == expected_copies
    assert all(cmd[0] == "wl-copy" for cmd in calls)


def test_clipboard_paste_preserves_chinese_and_never_sends_enter(monkeypatch):
    monkeypatch.setattr(desktop, "desktop_query", lambda _: {"address": "target"})
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    calls = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    result = desktop.WaylandClipboardCommitter("target").commit("中文完整输入。")
    assert result.committed
    assert calls[0][1]["input"] == "中文完整输入。"
    assert calls[1][0] == ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]


def test_orb_uses_active_window_monitor_and_logical_scale(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test")
    data = {
        "monitors": [{"id": 2, "x": 1280, "y": 1080, "width": 3840, "height": 2160, "scale": 2}],
        "activewindow": {"monitor": 2},
        "clients": [{"class": "Recordian Overlay", "address": "0x123", "size": [280, 224]}],
    }
    monkeypatch.setattr(desktop, "desktop_query", data.__getitem__)
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    monkeypatch.setattr(desktop.subprocess, "run", run)
    assert desktop.place_overlay_on_active_monitor()
    assert 'monitor="2"' in calls[0][-1]
    assert 'x=2100,y=1888' in calls[0][-1]
    assert 'mode="top"' in calls[0][-1]
