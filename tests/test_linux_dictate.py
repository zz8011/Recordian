import argparse
import json
from pathlib import Path

import pytest

from recordian.linux_dictate import (
    build_arecord_cmd,
    build_ffmpeg_record_cmd,
    build_parser,
    choose_record_backend,
    create_provider,
    transcribe_and_commit,
)


def test_build_ffmpeg_record_cmd_ogg() -> None:
    cmd = build_ffmpeg_record_cmd(
        ffmpeg_bin="/usr/bin/ffmpeg",
        output_path=Path("/tmp/in.ogg"),
        duration_s=3.5,
        sample_rate=16000,
        channels=1,
        input_device="default",
        record_format="ogg",
    )
    assert cmd[-5:] == ["-c:a", "libopus", "-b:a", "24k", "/tmp/in.ogg"]
    assert "-f" in cmd and "pulse" in cmd


def test_build_ffmpeg_record_cmd_wav() -> None:
    cmd = build_ffmpeg_record_cmd(
        ffmpeg_bin="/usr/bin/ffmpeg",
        output_path=Path("/tmp/in.wav"),
        duration_s=2.0,
        sample_rate=16000,
        channels=1,
        input_device="default",
        record_format="wav",
    )
    assert cmd[-3:] == ["-c:a", "pcm_s16le", "/tmp/in.wav"]


def test_build_ffmpeg_record_cmd_with_monitor_pipe() -> None:
    cmd = build_ffmpeg_record_cmd(
        ffmpeg_bin="/usr/bin/ffmpeg",
        output_path=Path("/tmp/in.wav"),
        duration_s=2.0,
        sample_rate=16000,
        channels=1,
        input_device="default",
        record_format="wav",
        enable_monitor=True,
    )
    assert "-filter_complex" in cmd
    assert "[0:a]asplit=2[record][monitor]" in cmd
    assert cmd[-5:] == ["-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]


def test_build_arecord_cmd_rounds_up_duration() -> None:
    cmd = build_arecord_cmd(
        output_path=Path("/tmp/in.wav"),
        duration_s=0.2,
        sample_rate=16000,
        channels=1,
    )
    assert cmd == [
        "arecord",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "1",
        "-d",
        "1",
        "/tmp/in.wav",
    ]


def test_build_arecord_cmd_uses_input_device_when_provided() -> None:
    cmd = build_arecord_cmd(
        output_path=Path("/tmp/in.wav"),
        duration_s=1.0,
        sample_rate=16000,
        channels=1,
        input_device="hw:1,0",
    )
    assert cmd == [
        "arecord",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "1",
        "-D",
        "hw:1,0",
        "-d",
        "1",
        "/tmp/in.wav",
    ]


def test_choose_record_backend_auto_fallback_arecord(monkeypatch) -> None:
    monkeypatch.setattr("recordian.linux_dictate._ffmpeg_supports_pulse", lambda _: False)
    monkeypatch.setattr("recordian.linux_dictate.which", lambda cmd: "/usr/bin/arecord" if cmd == "arecord" else None)
    assert choose_record_backend("auto", "/tmp/ffmpeg") == "arecord"


def test_commit_backend_rejects_unsupported_pynput_choice() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--commit-backend", "pynput"])


def test_parser_accepts_auto_hard_enter() -> None:
    parser = build_parser()
    args = parser.parse_args(["--auto-hard-enter"])
    assert args.auto_hard_enter is True


def test_parser_accepts_asr_context_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["--asr-context-preset", "meeting", "--asr-context", "OpenClaw, Recordian"])
    assert args.asr_context_preset == "meeting"
    assert args.asr_context == "OpenClaw, Recordian"


def test_parser_accepts_remote_paste_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--enable-remote-paste",
            "--remote-paste-host",
            "192.168.5.111",
            "--remote-paste-port",
            "24872",
            "--remote-paste-follow-deskflow-active-screen",
            "--deskflow-active-screen-path",
            "/tmp/deskflow-active.json",
            "--remote-paste-screen-name",
            "remote-screen",
        ]
    )
    assert args.enable_remote_paste is True
    assert args.remote_paste_host == "192.168.5.111"
    assert args.remote_paste_port == 24872
    assert args.remote_paste_follow_deskflow_active_screen is True
    assert args.deskflow_active_screen_path == "/tmp/deskflow-active.json"
    assert args.remote_paste_screen_name == "remote-screen"


def test_create_provider_merges_asr_preset_and_custom_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeProvider:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            captured.update(kwargs)

    class _PresetManager:
        def load_preset(self, name: str) -> str:
            assert name == "asr-meeting"
            return "会议术语A,会议术语B"

    monkeypatch.setattr("recordian.linux_dictate.QwenASRProvider", _FakeProvider)
    monkeypatch.setattr("recordian.preset_manager.PresetManager", _PresetManager)

    args = argparse.Namespace(
        asr_provider="qwen-asr",
        qwen_model="Qwen/Qwen3-ASR-0.6B",
        model="Qwen/Qwen3-ASR-1.7B",
        device="cpu",
        qwen_language="Chinese",
        qwen_max_new_tokens=512,
        asr_context_preset="meeting",
        asr_context="OpenClaw,Recordian",
    )
    create_provider(args)
    context = str(captured.get("context", ""))
    assert "会议术语A" in context
    assert "OpenClaw" in context


def test_create_provider_does_not_fallback_to_refine_default_preset(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeProvider:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            captured.update(kwargs)

    class _PresetManager:
        def load_preset(self, name: str) -> str:
            raise FileNotFoundError(name)

    monkeypatch.setattr("recordian.linux_dictate.QwenASRProvider", _FakeProvider)
    monkeypatch.setattr("recordian.preset_manager.PresetManager", _PresetManager)

    args = argparse.Namespace(
        asr_provider="qwen-asr",
        qwen_model="Qwen/Qwen3-ASR-0.6B",
        model="Qwen/Qwen3-ASR-1.7B",
        device="cpu",
        qwen_language="Chinese",
        qwen_max_new_tokens=512,
        asr_context_preset="default",
        asr_context="OpenClaw,Recordian",
    )
    create_provider(args)
    assert captured.get("context") == "OpenClaw,Recordian"


def test_stop_record_process_ffmpeg_uses_sigint() -> None:
    import signal
    from unittest.mock import MagicMock

    from recordian.linux_dictate import stop_record_process
    proc = MagicMock()
    proc.poll.return_value = None
    stop_record_process(proc, recorder_backend="ffmpeg-pulse")
    proc.send_signal.assert_called_once_with(signal.SIGINT)


def test_stop_record_process_arecord_uses_sigterm() -> None:
    import signal
    from unittest.mock import MagicMock

    from recordian.linux_dictate import stop_record_process
    proc = MagicMock()
    proc.poll.return_value = None
    stop_record_process(proc, recorder_backend="arecord")
    proc.send_signal.assert_called_once_with(signal.SIGTERM)


def test_stop_record_process_returns_quickly_when_process_exits() -> None:
    """进程快速退出时应立即返回，不等待完整超时"""
    import time
    from unittest.mock import MagicMock

    from recordian.linux_dictate import stop_record_process

    mock_proc = MagicMock()
    # 模拟进程在第 3 次 poll 时退出
    mock_proc.poll.side_effect = [None, None, 0]

    start = time.time()
    stop_record_process(mock_proc, recorder_backend="arecord")
    elapsed = time.time() - start

    # 应该在 0.3 秒内返回（3 次 poll × 0.1s），而非等待 2 秒
    assert elapsed < 0.5, f"停止耗时 {elapsed:.2f}s，应 < 0.5s"
    assert mock_proc.poll.call_count == 3


def test_stop_record_process_closes_monitor_stream_for_handle() -> None:
    from unittest.mock import MagicMock

    from recordian.linux_dictate import RecordProcessHandle, stop_record_process

    proc = MagicMock()
    proc.poll.return_value = 0
    monitor_stream = MagicMock()

    stop_record_process(
        RecordProcessHandle(process=proc, monitor_stream=monitor_stream),
        recorder_backend="ffmpeg-pulse",
    )

    monitor_stream.close.assert_called_once_with()


def test_transcribe_and_commit_includes_remote_paste_result(monkeypatch, tmp_path: Path) -> None:
    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return type("AsrResult", (), {"text": "远端文本"})()

    class _Committer:
        backend_name = "stdout"

        def commit(self, text: str):  # noqa: ANN001
            return type("CommitResult", (), {"backend": "stdout", "committed": True, "detail": "local_ok"})()

    captured: dict[str, object] = {}

    def _fake_remote(args, text: str, *, log=None):  # noqa: ANN001
        captured["text"] = text
        if callable(log):
            log("remote_ok")
        return {"enabled": True, "attempted": True, "sent": True, "host": "192.168.5.111", "detail": "ok"}

    monkeypatch.setattr("recordian.linux_dictate.send_remote_paste_from_args", _fake_remote)

    args = argparse.Namespace(
        enable_remote_paste=True,
        remote_paste_host="192.168.5.111",
        remote_paste_port=24872,
        remote_paste_timeout_s=3.0,
    )
    text, _latency, commit_info = transcribe_and_commit(
        args=args,
        provider=_Provider(),
        committer=_Committer(),
        audio_path=tmp_path / "sample.wav",
        hotwords=[],
        auto_hard_enter=False,
    )

    assert text == "远端文本"
    assert captured["text"] == "远端文本"
    assert commit_info["committed"] is True
    assert commit_info["remote_paste"]["sent"] is True


def test_transcribe_and_commit_routes_to_remote_only_when_deskflow_screen_matches(monkeypatch, tmp_path: Path) -> None:
    state_path = tmp_path / "active_screen.json"
    state_path.write_text(
        json.dumps({"screen": "remote-screen", "server_name": "server-screen", "updated_at": "2026-03-15T00:00:00Z"}),
        encoding="utf-8",
    )

    class _Provider:
        def transcribe_file(self, audio_path: Path, hotwords: list[str]):  # noqa: ANN001
            return type("AsrResult", (), {"text": "远端文本"})()

    class _Committer:
        backend_name = "stdout"

        def commit(self, text: str):  # noqa: ANN001
            raise AssertionError("remote-only route should skip local commit")

    monkeypatch.setattr(
        "recordian.linux_dictate.send_remote_paste_from_args",
        lambda args, text, *, log=None: {
            "enabled": True,
            "attempted": True,
            "sent": True,
            "host": "192.168.5.111",
            "detail": "ok",
            "routing_mode": "remote-only",
        },
    )

    args = argparse.Namespace(
        enable_remote_paste=True,
        remote_paste_host="192.168.5.111",
        remote_paste_port=24872,
        remote_paste_timeout_s=3.0,
        remote_paste_follow_deskflow_active_screen=True,
        deskflow_active_screen_path=str(state_path),
        remote_paste_screen_name="remote-screen",
    )
    text, _latency, commit_info = transcribe_and_commit(
        args=args,
        provider=_Provider(),
        committer=_Committer(),
        audio_path=tmp_path / "sample.wav",
        hotwords=[],
        auto_hard_enter=False,
    )

    assert text == "远端文本"
    assert commit_info["backend"] == "remote-paste"
    assert commit_info["committed"] is True
    assert commit_info["detail"] == "ok"
    assert commit_info["remote_paste"]["routing_mode"] == "remote-only"
