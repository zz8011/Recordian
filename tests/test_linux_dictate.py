from pathlib import Path

import pytest

from recordian.linux_dictate import build_arecord_cmd, build_ffmpeg_record_cmd, build_parser, choose_record_backend


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
