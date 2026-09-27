"""Reversible output-only muting, with a crash-recovery journal.

Enabled for the desktop service with RECORDIAN_MUTE_OUTPUT=1. Microphone
sources and volume levels are never changed. A process-held flock prevents a
cleanup helper or another recording from restoring a live session's outputs.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
from pathlib import Path


class OutputMute:
    def __init__(self, *, directory=None, run=None):
        self.directory = Path(directory or (
            Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "recordian-output"
        ))
        self.run = run or subprocess.run
        self.lock = None
        self.journal = self.directory / "muted.json"

    def _command(self, *args):
        return self.run(
            ["pactl", *args], check=True, capture_output=True, text=True,
            timeout=2, env={**os.environ, "LC_ALL": "C"},
        ).stdout

    def _acquire(self):
        if self.lock is not None:
            raise RuntimeError("Output mute lease already active")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = (self.directory / "lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._release()
            raise

    def _release(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def _restore(self):
        if not self.journal.exists():
            return
        original = json.loads(self.journal.read_text())
        current = {(s["index"], s["name"]): s for s in json.loads(self._command("-f", "json", "list", "sinks"))}
        failures = []
        for sink in original:
            # A reconnected/replaced output must not inherit a stale restore.
            live = current.get((sink["index"], sink["name"]))
            if live and live["mute"]:
                try:
                    self._command("set-sink-mute", str(sink["index"]), "0")
                except (OSError, subprocess.SubprocessError):
                    failures.append(sink)
        if failures:
            # Successful restores no longer belong to us: do not unmute them
            # again if the user changes their state before the recovery retry.
            temporary = self.journal.with_suffix(".tmp")
            temporary.write_text(json.dumps(failures))
            temporary.replace(self.journal)
            raise RuntimeError("Could not restore audio output; recovery journal retained")
        self.journal.unlink(missing_ok=True)

    def begin(self):
        self._acquire()
        try:
            self._restore()
            sinks = json.loads(self._command("-f", "json", "list", "sinks"))
            changed = [{"index": s["index"], "name": s["name"]} for s in sinks if not s["mute"]]
            # Persist before any mutation, including partially failed starts.
            temporary = self.journal.with_suffix(".tmp")
            temporary.write_text(json.dumps(changed))
            temporary.replace(self.journal)
            for sink in changed:
                self._command("set-sink-mute", str(sink["index"]), "1")
        except BaseException:
            try:
                self._restore()
            finally:
                self._release()
            raise

    def end(self):
        if self.lock is None:
            return
        try:
            self._restore()
        finally:
            self._release()

    def recover(self):
        self._acquire()
        try:
            self._restore()
        finally:
            self._release()


def begin_output_mute():
    if os.environ.get("RECORDIAN_MUTE_OUTPUT") != "1":
        return None
    lease = OutputMute()
    lease.begin()
    return lease


def main():
    parser = argparse.ArgumentParser(description="Restore Recordian-muted outputs after a stopped service")
    parser.add_argument("action", choices=["restore"])
    parser.parse_args()
    try:
        OutputMute().recover()
    except BlockingIOError:
        pass  # A live recording owns the lease; never restore its outputs.


if __name__ == "__main__":
    main()
