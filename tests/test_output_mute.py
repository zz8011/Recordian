import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from recordian.output_mute import OutputMute, begin_output_mute


class Audio:
    def __init__(self):
        self.sinks = [
            {"index": 1, "name": "speakers", "mute": False, "volume": 0.67},
            {"index": 2, "name": "headphones", "mute": True, "volume": 0.42},
        ]
        self.commands = []
        self.fail = None

    def __call__(self, command, **kwargs):
        args = command[1:]
        self.commands.append(args)
        if self.fail and args == self.fail:
            self.fail = None
            raise subprocess.CalledProcessError(1, command)
        if args == ["-f", "json", "list", "sinks"]:
            return SimpleNamespace(stdout=json.dumps(self.sinks))
        assert args[0] == "set-sink-mute"  # No source/volume mutations permitted.
        sink = next(s for s in self.sinks if str(s["index"]) == args[1])
        sink["mute"] = args[2] == "1"
        return SimpleNamespace(stdout="")


def test_restore_preserves_initial_mute_and_volume(tmp_path):
    audio = Audio()
    original = deepcopy(audio.sinks)
    lease = OutputMute(directory=tmp_path, run=audio)
    lease.begin()
    assert all(s["mute"] for s in audio.sinks)
    lease.end()
    assert audio.sinks == original
    assert not lease.journal.exists()
    lease.end()  # Idempotent; no extra unmute.


def test_live_lease_cannot_be_recovered_or_double_started(tmp_path):
    audio = Audio()
    lease = OutputMute(directory=tmp_path, run=audio)
    lease.begin()
    with pytest.raises(RuntimeError, match="already active"):
        lease.begin()
    with pytest.raises(BlockingIOError):
        OutputMute(directory=tmp_path, run=audio).recover()
    assert audio.sinks[0]["mute"]
    lease.end()


def test_crash_journal_restored_by_new_process_owner(tmp_path):
    audio = Audio()
    lease = OutputMute(directory=tmp_path, run=audio)
    lease.begin()
    lease._release()  # Kernel closes flock on a process crash.
    OutputMute(directory=tmp_path, run=audio).recover()
    assert not audio.sinks[0]["mute"]
    assert audio.sinks[1]["mute"]


def test_partial_start_failure_rolls_back(tmp_path):
    audio = Audio()
    audio.sinks[1]["mute"] = False
    audio.fail = ["set-sink-mute", "2", "1"]
    lease = OutputMute(directory=tmp_path, run=audio)
    with pytest.raises(subprocess.CalledProcessError):
        lease.begin()
    assert not any(s["mute"] for s in audio.sinks)
    assert lease.lock is None
    assert not lease.journal.exists()


def test_restore_failure_retains_journal_for_service_cleanup(tmp_path):
    audio = Audio()
    lease = OutputMute(directory=tmp_path, run=audio)
    lease.begin()
    audio.fail = ["set-sink-mute", "1", "0"]
    with pytest.raises(RuntimeError, match="journal retained"):
        lease.end()
    assert lease.journal.exists()
    assert lease.lock is None
    OutputMute(directory=tmp_path, run=audio).recover()
    assert not audio.sinks[0]["mute"]


def test_reconnected_sink_is_not_unmuted(tmp_path):
    audio = Audio()
    lease = OutputMute(directory=tmp_path, run=audio)
    lease.begin()
    audio.sinks[0]["index"] = 99
    lease.end()
    assert audio.sinks[0]["mute"]


def test_disabled_does_not_touch_audio(monkeypatch):
    monkeypatch.delenv("RECORDIAN_MUTE_OUTPUT", raising=False)
    assert begin_output_mute() is None


def test_partial_restore_forgets_outputs_already_restored(tmp_path):
    audio = Audio()
    audio.sinks[1]["mute"] = False
    lease = OutputMute(directory=tmp_path, run=audio)
    lease.begin()
    audio.fail = ["set-sink-mute", "2", "0"]
    with pytest.raises(RuntimeError):
        lease.end()
    audio.sinks[0]["mute"] = True  # User muted an already-restored speaker.
    OutputMute(directory=tmp_path, run=audio).recover()
    assert audio.sinks[0]["mute"] is True
    assert audio.sinks[1]["mute"] is False
