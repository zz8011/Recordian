"""Native fcitx5 addon acceptance test (real GUI, isolated).

Runs the real compiled recordian-commit addon against a real GTK3 entry
inside a private Xvfb display + private dbus session. Proves the
Begin/Update/Commit/Cancel/FocusOut contract end-to-end (no mocks).

Gated behind RECORDIAN_NATIVE_GUI=1 so regular CI never touches X11:

    RECORDIAN_NATIVE_GUI=1 pytest tests/test_fcitx_native.py -v

Layout (all paths self-owned; nothing touches the user's session):
- Xvfb on a display allocated via `-displayfd` (killed by PID on exit).
- dbus-run-session provides a private session bus.
- RECORDIAN_NATIVE_TMP (tempfile.mkdtemp) holds XDG config/data/runtime.
- The addon is built into <coordination-root>/native-build (override with
  RECORDIAN_NATIVE_BUILD). GLM owns fcitx/recordian-commit source; this
  test only compiles it, never edits it.

Driver stdout/stderr is teed to <coordination-root>/kimi-native.validation.log
as raw evidence.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "native" / "drive_native_session.py"

# Persisted validation log: RECORDIAN_NATIVE_VALIDATION_LOG overrides; default
# lives inside the build dir so the test is portable (no assumptions about the
# surrounding folder layout). Same for the build dir: RECORDIAN_NATIVE_BUILD
# overrides; default is a sibling "native-build" of the repo if writable.
DEFAULT_BUILD = REPO.parent / "native-build"

pytestmark = pytest.mark.skipif(
    os.environ.get("RECORDIAN_NATIVE_GUI") != "1",
    reason="native GUI test; set RECORDIAN_NATIVE_GUI=1 to run (uses private Xvfb + dbus)",
)


def _start_xvfb() -> tuple[subprocess.Popen, str]:
    """Start Xvfb on a kernel-allocated free display; return (proc, DISPLAY)."""
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    try:
        data = b""
        deadline = time.monotonic() + 15
        while b"\n" not in data:
            if time.monotonic() > deadline:
                raise RuntimeError("Xvfb did not report a display number")
            if proc.poll() is not None:
                raise RuntimeError(f"Xvfb exited early rc={proc.returncode}")
            chunk = os.read(read_fd, 32)
            if not chunk:
                raise RuntimeError("Xvfb closed displayfd without a display number")
            data += chunk
        display = f":{int(data.splitlines()[0].strip())}"
    finally:
        os.close(read_fd)
    # Wait until the server actually answers.
    env = {**os.environ, "DISPLAY": display}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Xvfb died rc={proc.returncode}")
        if subprocess.run(
            ["xdotool", "getdisplaygeometry"], env=env, capture_output=True
        ).returncode == 0:
            return proc, display
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError("Xvfb never became ready")


def _find_gtk_python() -> str:
    """System python3 with PyGObject GTK3 (the venv may not have gi)."""
    for candidate in ("/usr/bin/python3", shutil.which("python3") or ""):
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-c", "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError("no python3 with PyGObject GTK3 found (need gir1.2-gtk-3.0)")


def _ensure_addon_build() -> Path:
    build = Path(os.environ.get("RECORDIAN_NATIVE_BUILD", DEFAULT_BUILD))
    src = REPO / "fcitx" / "recordian-commit"
    if not (build / "CMakeCache.txt").exists():
        subprocess.run(
            ["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=RelWithDebInfo"],
            check=True, capture_output=True, text=True,
        )
    # Always rebuild (incremental): never validate new source with a stale
    # binary. make only re-links when recordian-commit.cpp actually changed.
    subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True, text=True)
    so = build / "librecordian-commit.so"
    if not so.exists():
        raise RuntimeError(f"addon build did not produce {so}")
    return build


def test_native_fcitx_session_contract():
    missing = [tool for tool in ("Xvfb", "dbus-run-session", "xdotool", "busctl", "fcitx5", "cmake") if not shutil.which(tool)]
    if missing:
        pytest.fail(f"RECORDIAN_NATIVE_GUI=1 but required tools are missing: {missing}")

    build = _ensure_addon_build()
    import hashlib

    src_sha = hashlib.sha256(
        (REPO / "fcitx" / "recordian-commit" / "recordian-commit.cpp").read_bytes()
    ).hexdigest()[:12]
    so_sha = hashlib.sha256((build / "librecordian-commit.so").read_bytes()).hexdigest()[:12]
    validation_log = Path(
        os.environ.get("RECORDIAN_NATIVE_VALIDATION_LOG", build / "native-validation.log")
    )
    gtk_py = _find_gtk_python()
    tmp = tempfile.mkdtemp(prefix="recordian-native-")
    for sub in ("config", "data", "cache", "home"):
        os.makedirs(os.path.join(tmp, sub), exist_ok=True)
    runtime = os.path.join(tmp, "run")
    os.makedirs(runtime, exist_ok=True)
    os.chmod(runtime, 0o700)

    xvfb, display = _start_xvfb()
    try:
        env = {
            **os.environ,
            "DISPLAY": display,
            "HOME": os.path.join(tmp, "home"),
            "XDG_CONFIG_HOME": os.path.join(tmp, "config"),
            "XDG_DATA_HOME": os.path.join(tmp, "data"),
            "XDG_CACHE_HOME": os.path.join(tmp, "cache"),
            "XDG_RUNTIME_DIR": runtime,
            "RECORDIAN_NATIVE_TMP": tmp,
            "RECORDIAN_NATIVE_BUILD": str(build),
            "RECORDIAN_CANDIDATE_SRC": str(REPO / "src"),
            "RECORDIAN_NATIVE_PY": gtk_py,
            "PYTHONPATH": str(REPO / "src"),
            "GTK_IM_MODULE": "fcitx",
            "QT_IM_MODULE": "fcitx",
            "XMODIFIERS": "@im=fcitx",
        }
        # Private bus inside this Xvfb. Do not keep the host session bus or Wayland.
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        focus = os.environ.get("RECORDIAN_NATIVE_FOCUS", "")
        run = subprocess.run(
            ["dbus-run-session", "--", sys.executable, str(DRIVER)],
            env=env,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=400 if focus == "browser-segments" else 300,
        )
        raw = run.stdout + ("\n--- stderr ---\n" + run.stderr if run.stderr else "")
        validation_log.write_text(
            f"# kimi-native validation log\n# display={display} tmp={tmp} build={build}\n"
            f"# recordian-commit.cpp sha256[0:12]={src_sha} librecordian-commit.so sha256[0:12]={so_sha}\n"
            f"# driver rc={run.returncode}\n\n{raw}",
            encoding="utf-8",
        )
        scenarios = [line for line in run.stdout.splitlines() if "SCENARIO " in line or "VERDICT" in line]
        summary = "\n".join(scenarios[-25:])
        # LIMITATION results are explicit expected-platform-gaps and do not
        # fail the suite; FAIL results do. The driver exits nonzero on FAIL.
        assert run.returncode == 0, (
            f"native session driver failed rc={run.returncode}; "
            f"full log: {validation_log}\n{summary}"
        )
        if focus == "browser-segments":
            assert "SCENARIO s10b_browser_textarea_segments_once: PASS" in run.stdout, summary
            assert "SCENARIO s14b_browser_contenteditable_segments_once: PASS" in run.stdout, summary
            assert "SCENARIO s16_editor_commit_once: SKIP" in run.stdout, summary
            assert "SCENARIO s17_real_asr_worker_commit_once: SKIP" in run.stdout, summary
        else:
            assert "SCENARIO s1_commit_unicode_once: PASS" in run.stdout, summary
            if focus == "gtk-segments":
                assert "SCENARIO s30_empty_surround_many_preedits_then_segment: PASS" in run.stdout, summary
                assert "SCENARIO s31_unknown_before_unrelated_prefix_stays_stale: PASS" in run.stdout, summary
                assert "SCENARIO s32_foreign_key_before_empty_surround_commit_stale: PASS" in run.stdout, summary
    finally:
        # Only processes this test started: Xvfb by PID. fcitx5 and the GTK
        # apps are children of the driver and already reaped by it; the
        # private dbus daemon exits with dbus-run-session.
        if xvfb.poll() is None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
                xvfb.wait(timeout=5)
