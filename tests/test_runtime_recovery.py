"""回归测试：报错可见性、后端自动重启、ASR 重试与托盘单实例。

背景（FAILURES.md 2026-09-13）：语音输入「总是报错 / 后端停止」时，错误事件只更新
overlay，不落 stderr/journald/日志文件，事后完全无从定位。这里把新加的可观测性与
自愈行为固定下来。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from recordian.audio import write_wav_mono_f32
from recordian.backend_manager import BackendManager
from recordian.providers.http_cloud import HttpCloudProvider
from recordian.tray_app import (
    AUTO_RESTART_DELAYS_S,
    TrayApp,
    UiState,
    acquire_single_instance_lock,
)

pytest.importorskip("requests")


# ---------------------------------------------------------------------------
# BackendManager：区分「主动停止」与「非预期退出」
# ---------------------------------------------------------------------------

def _make_manager() -> tuple[BackendManager, queue.Queue[dict[str, object]]]:
    events: queue.Queue[dict[str, object]] = queue.Queue()
    manager = BackendManager(
        config_path=Path("/tmp/dsh-runtime-recovery.json"),
        events=events,
        on_state_change=lambda *_: None,
        on_menu_update=lambda: None,
    )
    return manager, events


class _FakeProc:
    def __init__(self, *, alive: bool = True, returncode: int = 0) -> None:
        self._alive = alive
        self._returncode = returncode
        self.pid = 4242

    def poll(self) -> int | None:
        return None if self._alive else self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        return self._returncode


def test_stop_marks_backend_exit_as_intentional(monkeypatch) -> None:
    monkeypatch.setattr("recordian.backend_manager._terminate_backend_process", lambda *_, **__: None)
    manager, events = _make_manager()
    manager.proc = _FakeProc()

    manager.stop()

    assert manager._intentional_stop is True
    assert events.get_nowait()["event"] == "stopped"

    # 随后 _wait 线程观察到的退出应被标记为主动停止
    manager.proc = _FakeProc(alive=False, returncode=0)
    manager._wait()
    event = events.get_nowait()
    assert event["event"] == "backend_exited"
    assert event["intentional"] is True


def test_exit_event_flags_unexpected_exit() -> None:
    manager, events = _make_manager()
    manager.proc = _FakeProc(alive=False, returncode=1)

    manager._wait()

    event = events.get_nowait()
    assert event["event"] == "backend_exited"
    assert event["code"] == 1
    assert event["intentional"] is False


def test_restart_marks_old_process_exit_as_intentional() -> None:
    """restart 后旧进程的退出事件不能触发托盘的「意外退出」自动重启。"""
    manager, events = _make_manager()
    old_proc = _FakeProc(alive=False, returncode=0)
    new_proc = _FakeProc()
    manager._intentional_stop = False
    manager.proc = new_proc

    manager._wait(old_proc)

    event = events.get_nowait()
    assert event["event"] == "backend_exited"
    assert event["intentional"] is True


# ---------------------------------------------------------------------------
# 托盘：错误可见 + 非预期退出自动重启
# ---------------------------------------------------------------------------

class _HeadlessRoot:
    def __init__(self) -> None:
        self.after_calls: list[tuple[int, Any]] = []
        self._cancelled: list[Any] = []

    def after(self, delay: int, callback=None):  # noqa: ANN001
        self.after_calls.append((delay, callback))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, after_id) -> None:  # noqa: ANN001
        self._cancelled.append(after_id)


class _FakeBackend:
    def __init__(self, *, alive: bool = False) -> None:
        self.proc: object | None = _FakeProc(alive=alive) if alive else None
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.proc = _FakeProc(alive=True)


class _FakeOverlay:
    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []

    def set_state(self, state: str, detail: str = "") -> None:
        self.states.append((state, detail))


def _make_tray_app(*, backend: _FakeBackend) -> TrayApp:
    app = TrayApp.__new__(TrayApp)
    app.args = SimpleNamespace(config_path="/tmp/dsh-runtime-recovery.json", no_auto_start=True)
    app.config_path = Path("/tmp/dsh-runtime-recovery.json")
    app.state = UiState()
    app.events = queue.Queue()
    app.root = _HeadlessRoot()
    app.backend = backend
    app.overlay = _FakeOverlay()
    app._config_cache = None
    app._config_cache_mtime = 0.0
    app._toggle_lock = threading.Lock()
    app._update_tray_menu = lambda: None
    app._off_sound_after_id = None
    app._off_cue_armed = False
    app._warmup_done = False
    app._quitting = False
    app._auto_restart_attempts = 0
    app._auto_restart_after_id = None
    return app


def test_error_event_is_logged_to_stderr(capsys) -> None:
    app = _make_tray_app(backend=_FakeBackend())

    app._handle_event({"event": "error", "error": "HTTPError: 500 Server Error"})

    err = capsys.readouterr().err
    assert "[recordian-tray] error: HTTPError: 500 Server Error" in err


def test_log_event_is_logged_to_stderr(capsys) -> None:
    app = _make_tray_app(backend=_FakeBackend())

    app._handle_event({"event": "log", "message": "text_refine_failed: RuntimeError: 404"})

    err = capsys.readouterr().err
    assert "text_refine_failed: RuntimeError: 404" in err


def test_unexpected_backend_exit_schedules_restart(monkeypatch) -> None:
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "recordian.tray_app.notify_desktop",
        lambda title, body, **_: notifications.append((title, body)),
    )
    backend = _FakeBackend(alive=False)
    app = _make_tray_app(backend=backend)

    app._handle_event({"event": "backend_exited", "code": 1, "intentional": False})

    # 托盘应排定一次自动重启，并通知用户
    assert app._auto_restart_attempts == 1
    assert app._auto_restart_after_id is not None
    assert notifications and "自动重启" in notifications[0][1]

    # 执行排定的回调 -> 后端被重新拉起，计数在下一次 ready 时清零
    _delay_ms, callback = app.root.after_calls[-1]
    assert callback is not None
    callback()
    assert backend.start_calls == 1
    assert app._backend_alive() is True

    app._handle_event({"event": "ready", "hotkey": "<ctrl_r>"})
    assert app._auto_restart_attempts == 0


def test_intentional_backend_stop_does_not_restart(monkeypatch) -> None:
    monkeypatch.setattr("recordian.tray_app.notify_desktop", lambda *_, **__: None)
    backend = _FakeBackend(alive=False)
    app = _make_tray_app(backend=backend)

    app._handle_event({"event": "backend_exited", "code": 0, "intentional": True})
    app._handle_event({"event": "stopped"})

    assert backend.start_calls == 0
    assert app.state.backend_running is False


def test_restart_budget_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr("recordian.tray_app.notify_desktop", lambda *_, **__: None)
    backend = _FakeBackend(alive=False)
    app = _make_tray_app(backend=backend)
    app._restart_backend = lambda: None  # 模拟后端始终起不来

    for _ in range(len(AUTO_RESTART_DELAYS_S) + 3):
        app._schedule_backend_restart("exit_code=1")

    assert app._auto_restart_attempts == len(AUTO_RESTART_DELAYS_S)


def test_backend_exit_event_is_logged(capsys) -> None:
    app = _make_tray_app(backend=_FakeBackend(alive=False))

    app._handle_event({"event": "backend_exited", "code": 137, "intentional": True})

    err = capsys.readouterr().err
    assert "backend_exited code=137 intentional=True" in err


def test_single_instance_lock_rejects_second_holder(tmp_path: Path) -> None:
    config_path = tmp_path / "hotkey.json"
    config_path.write_text("{}", encoding="utf-8")

    first_fd = acquire_single_instance_lock(config_path)
    assert first_fd is not None
    try:
        with pytest.raises(RuntimeError, match="已在运行"):
            acquire_single_instance_lock(config_path)
    finally:
        import os

        os.close(first_fd)


def test_single_instance_lock_degrades_when_unwritable(tmp_path: Path, monkeypatch) -> None:
    """锁文件写不出来时不能让整个语音输入起不来。"""

    def _deny(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("os.open", _deny)

    assert acquire_single_instance_lock(tmp_path / "hotkey.json") is None


# ---------------------------------------------------------------------------
# ASR：瞬时失败重试一次 + 可读错误
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, object]:
        return self._payload


def _wav(tmp_path: Path) -> Path:
    path = tmp_path / "demo.wav"
    write_wav_mono_f32(path, [0.0] * 1600, sample_rate=16000)
    return path


def test_asr_retries_once_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("recordian.providers.http_cloud.TRANSCRIBE_RETRY_DELAY_S", 0.0)
    provider = HttpCloudProvider("http://localhost:9999/asr", model_name="mega-asr-vllm")
    responses = [
        _FakeResponse(503, text='{"error":"no running instances"}'),
        _FakeResponse(200, {"text": "重试成功"}),
    ]
    calls: list[int] = []

    def _post(*_args, **_kwargs):
        calls.append(len(calls))
        return responses.pop(0)

    monkeypatch.setattr("requests.post", _post)
    result = provider.transcribe_file(_wav(tmp_path), hotwords=[])

    assert result.text == "重试成功"
    assert len(calls) == 2


def test_asr_reports_model_not_running_clearly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("recordian.providers.http_cloud.TRANSCRIBE_RETRY_DELAY_S", 0.0)
    provider = HttpCloudProvider("http://localhost:9999/asr", model_name="mega-asr-vllm")
    body = '{"error":{"message":"Model not found or no running instances available","code":404}}'
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(404, text=body))

    with pytest.raises(RuntimeError) as excinfo:
        provider.transcribe_file(_wav(tmp_path), hotwords=[])

    message = str(excinfo.value)
    assert "404" in message
    assert "mega-asr-vllm" in message


def test_asr_reports_connection_failure_clearly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("recordian.providers.http_cloud.TRANSCRIBE_RETRY_DELAY_S", 0.0)
    provider = HttpCloudProvider("http://192.168.5.111/v1/audio/transcriptions", model_name="mega-asr-vllm")

    class _ConnectionError(Exception):
        pass

    _ConnectionError.__name__ = "ConnectionError"

    def _raise(*_args, **_kwargs):
        raise _ConnectionError("connection refused")

    monkeypatch.setattr("requests.post", _raise)

    with pytest.raises(RuntimeError) as excinfo:
        provider.transcribe_file(_wav(tmp_path), hotwords=[])

    assert "无法连接 ASR 后端" in str(excinfo.value)


def test_asr_client_error_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("recordian.providers.http_cloud.TRANSCRIBE_RETRY_DELAY_S", 0.0)
    provider = HttpCloudProvider("http://localhost:9999/asr", model_name="mega-asr-vllm")
    calls: list[int] = []

    def _post(*_args, **_kwargs):
        calls.append(len(calls))
        return _FakeResponse(401, text="unauthorized")

    monkeypatch.setattr("requests.post", _post)

    with pytest.raises(Exception):  # noqa: B017 - 401 由 raise_for_status 抛出
        provider.transcribe_file(_wav(tmp_path), hotwords=[])

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 文字精炼：模型实例没在跑时要报得能看懂
# ---------------------------------------------------------------------------

def test_refine_model_not_running_message_is_actionable() -> None:
    from recordian.providers.cloud_llm_refiner import CloudLLMRefiner

    refiner = CloudLLMRefiner(
        api_base="http://192.168.5.111/v1",
        api_key="k",
        model="Qwen3.6-35B-A3B-UD-Q4_K_XL",
        api_format="openai",
        timeout=30,
    )
    response = _FakeResponse(
        404,
        text='{"error":{"message":"Model not found or no running instances available","code":404}}',
    )

    with pytest.raises(RuntimeError) as excinfo:
        refiner._raise_on_error(response)

    message = str(excinfo.value)
    assert "没有运行实例" in message
    assert "Qwen3.6-35B-A3B-UD-Q4_K_XL" in message
    assert "GPUStack" in message


def test_refine_connection_error_message(monkeypatch) -> None:
    from recordian.providers import cloud_llm_refiner as module

    class _ConnectionError(Exception):
        pass

    _ConnectionError.__name__ = "ConnectionError"

    def _raise(*_args, **_kwargs):
        raise _ConnectionError("connection refused")

    monkeypatch.setattr("requests.post", _raise)
    refiner = module.CloudLLMRefiner(
        api_base="http://192.168.5.111/v1", api_key="k", model="m", api_format="openai", timeout=5
    )

    with pytest.raises(RuntimeError, match="无法连接文字精炼后端"):
        refiner.refine("你好")


# ---------------------------------------------------------------------------
# 本地 llama.cpp 精炼器：preset 必须原样喂给模型
# ---------------------------------------------------------------------------

def test_llamacpp_refiner_renders_preset_prompt() -> None:
    from recordian.providers.llamacpp_text_refiner import LlamaCppTextRefiner

    refiner = LlamaCppTextRefiner(
        "/tmp/does-not-need-to-exist.gguf",
        prompt_template="规则A：删掉口水词。\n原文：\n{text}\n",
        enable_thinking=False,
    )

    messages = refiner._build_chat_messages("嗯这个这个可以")

    assert messages is not None
    content = messages[0]["content"]
    assert "规则A：删掉口水词。" in content  # preset 规则必须进 prompt
    assert "嗯这个这个可以" in content
    assert content.rstrip().endswith("/no_think")  # 非 thinking 模式


def test_llamacpp_refiner_keeps_thinking_when_enabled() -> None:
    from recordian.providers.llamacpp_text_refiner import LlamaCppTextRefiner

    refiner = LlamaCppTextRefiner(
        "/tmp/does-not-need-to-exist.gguf",
        prompt_template="原文：\n{text}",
        enable_thinking=True,
    )

    messages = refiner._build_chat_messages("你好")

    assert messages is not None
    assert "/no_think" not in messages[0]["content"]


def test_llamacpp_refiner_without_preset_uses_fewshot_fallback() -> None:
    from recordian.providers.llamacpp_text_refiner import LlamaCppTextRefiner

    refiner = LlamaCppTextRefiner("/tmp/does-not-need-to-exist.gguf", prompt_template=None)

    assert refiner._build_chat_messages("你好") is None
    assert "输入：" in refiner._build_fewshot_prompt("你好")


def test_llamacpp_refiner_extracts_chat_and_completion_text() -> None:
    from recordian.providers.llamacpp_text_refiner import LlamaCppTextRefiner

    assert LlamaCppTextRefiner._extract_chat_text({"choices": [{"message": {"content": " 好 "}}]}) == "好"
    assert LlamaCppTextRefiner._extract_completion_text({"choices": [{"text": " 好 "}]}) == "好"
    assert LlamaCppTextRefiner._extract_chat_text({}) == ""
    assert LlamaCppTextRefiner._extract_completion_text({"choices": []}) == ""


# ---------------------------------------------------------------------------
# 录音 ffmpeg 的 stderr 不再被丢弃
# ---------------------------------------------------------------------------

def test_record_process_stderr_goes_to_log_file(tmp_path: Path, monkeypatch) -> None:
    from recordian import linux_dictate

    log_path = tmp_path / "record-ffmpeg.log"
    monkeypatch.setenv("RECORDIAN_RECORD_LOG", str(log_path))
    monkeypatch.setattr(linux_dictate, "_record_stderr_log", None)

    captured: dict[str, object] = {}

    class _Popen:
        def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001
            captured["cmd"] = cmd
            captured["stderr"] = kwargs.get("stderr")
            self.stdout = None
            self.pid = 1234
            self.returncode = 0

        def poll(self):  # noqa: ANN201
            return 0

    monkeypatch.setattr(linux_dictate.subprocess, "Popen", _Popen)
    args = SimpleNamespace(
        record_backend="ffmpeg-pulse",
        sample_rate=16000,
        channels=1,
        input_device="default",
        record_format="wav",
    )

    linux_dictate.start_record_process(
        args=args,
        ffmpeg_bin="ffmpeg",
        recorder_backend="ffmpeg-pulse",
        output_path=tmp_path / "input.wav",
        duration_s=2.0,
        enable_monitor=True,
    )

    sink = captured["stderr"]
    assert sink is not None
    assert getattr(sink, "name", "") == str(log_path)
    assert "ffmpeg-pulse" in log_path.read_text(encoding="utf-8")
    linux_dictate._close_record_stderr_log()
