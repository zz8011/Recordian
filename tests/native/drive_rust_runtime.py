"""Private-bus GTK acceptance using production Rust client methods exclusively."""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import drive_native_session as fixture


def wait_text(app, expected, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fixture.latest_text(app) == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"GTK buffer mismatch: {fixture.latest_text(app)!r} != {expected!r}")


def main():
    from recordian.linux_commit import FcitxCommitter
    from recordian.native_core import status

    assert status()["backend"] == "rust"
    fixture.setup_layout()
    os.environ.update(fixture.child_env())
    apps = []
    with open(Path(fixture.TMP) / "fcitx5.log", "w", encoding="utf-8") as log:
        fixture.fcitx = subprocess.Popen(["fcitx5"], stdout=log, stderr=subprocess.STDOUT)
        try:
            assert fixture.wait_addon(), "private addon did not start"
            app = fixture.App([str(Path(__file__).with_name("gtk_entry_app.py")), "rust"])
            other = fixture.App([str(Path(__file__).with_name("gtk_entry_app.py")), "--other", "other"])
            apps.extend((app, other))
            assert app.wait_line("READY") and other.wait_line("WID ")
            assert fixture.focus_window(app.wid)
            time.sleep(0.5)
            session = fixture.begin_with_focus_retry(FcitxCommitter(), app.wid)
            text = "Rust 听写测试 🎙️ 中文 English 123"
            assert session.update_preedit(text).committed
            deadline = time.monotonic() + 3
            while not any(value == text for _, value in app.preedits()) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert any(value == text for _, value in app.preedits()), "real GTK did not display preedit"
            assert session.commit(text).committed
            wait_text(app, text)
            assert not session.commit(text).committed
            wait_text(app, text)
            print("RUST_CASE unicode_preedit_commit_once PASS", flush=True)

            session = fixture.begin_with_focus_retry(FcitxCommitter(), app.wid)
            assert session.update_preedit("取消测试不会写入").committed
            assert session.cancel().outcome == "cancelled"
            assert not session.commit("取消后禁止写入").committed
            wait_text(app, text)
            print("RUST_CASE cancel_no_replay PASS", flush=True)

            session = fixture.begin_with_focus_retry(FcitxCommitter(), app.wid)
            assert session.update_preedit("焦点测试").committed
            assert fixture.focus_window(other.wid)
            time.sleep(0.3)
            rejected = session.commit("失焦后禁止写入")
            assert not rejected.committed and rejected.outcome == "stale", rejected
            assert "失焦后禁止写入" not in fixture.latest_text(app)
            assert "失焦后禁止写入" not in fixture.latest_text(other)
            assert fixture.focus_window(app.wid)
            print("RUST_CASE focus_loss_stale PASS", flush=True)

            wav = os.environ.get("RECORDIAN_RUST_ASR_WAV")
            if wav:
                import numpy as np

                from recordian.linux_dictate import RecordProcessHandle
                from recordian.providers.confucius_asr import ConfuciusASRProvider
                from recordian.realtime_asr import _start_realtime_asr_worker

                with wave.open(wav, "rb") as source:
                    assert (source.getnchannels(), source.getframerate(), source.getsampwidth()) == (1, 16000, 2)
                    raw = (
                        np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
                    ).tobytes()

                class PacedStream(io.BytesIO):
                    def read(self, size=-1):
                        block = super().read(size)
                        if block:
                            time.sleep(len(block) / 64000)
                        return block

                baseline = fixture.latest_text(app)
                mark = len(app.preedit_events)
                token = Path(os.environ["RECORDIAN_RUST_ASR_TOKEN_FILE"]).read_text().strip()
                provider = ConfuciusASRProvider("ws://127.0.0.1:8321", api_key=token, timeout_s=80)
                idle_process = subprocess.Popen(["sleep", "120"])
                try:
                    handle = RecordProcessHandle(
                        process=idle_process,
                        monitor_stream=PacedStream(raw),
                        monitor_sample_rate=16000,
                        monitor_channels=1,
                    )
                    args = SimpleNamespace(
                        enable_streaming_commit=True,
                        sample_rate=16000,
                        channels=1,
                        debug_diagnostics=False,
                        enable_semif_correction=False,
                        semif_endpoint="",
                        semif_timeout_s=0.12,
                        hotword=[],
                        asr_context="",
                        hotword_replacement=[],
                    )
                    worker = _start_realtime_asr_worker(
                        args=args,
                        provider=provider,
                        record_handle=handle,
                        committer=FcitxCommitter(),
                        enable_local_commit=True,
                        auto_hard_enter=False,
                        resolve_hotwords=list,
                        normalize_final_text=lambda value: value,
                        on_state=lambda _: None,
                        refine_enabled=False,
                    )
                    assert worker is not None
                    worker.thread.join(timeout=80)
                    assert not worker.thread.is_alive(), "real model worker timed out"
                    assert worker.outcome == "committed" and worker.final_text, (worker.outcome, worker.error)
                    wait_text(app, baseline + worker.final_text)
                    assert len({value for _, value in app.preedits(mark) if value}) >= 2
                    print(
                        "RUST_CASE real_model_to_gtk_once PASS "
                        + json.dumps(
                            {
                                "final_chars": len(worker.final_text),
                                "preedit_states": len({value for _, value in app.preedits(mark) if value}),
                            }
                        ),
                        flush=True,
                    )
                finally:
                    idle_process.terminate()
                    idle_process.wait(timeout=5)
            print("RUST_RUNTIME PASS", flush=True)
            return 0
        finally:
            for app in apps:
                app.stop()
            fixture.fcitx.terminate()
            try:
                fixture.fcitx.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fixture.fcitx.kill()
                fixture.fcitx.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
