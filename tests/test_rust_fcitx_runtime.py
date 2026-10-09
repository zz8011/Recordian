"""Production Rust transport against real GTK/Fcitx on a private display/bus."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RECORDIAN_RUST_GUI") != "1", reason="set RECORDIAN_RUST_GUI=1 for isolated Rust GTK acceptance"
)


def test_rust_fcitx_runtime():
    from test_fcitx_native import _ensure_addon_build, _find_gtk_python, _start_xvfb

    root = Path(__file__).resolve().parents[1]
    build = _ensure_addon_build()
    xvfb, display = _start_xvfb()
    try:
        with tempfile.TemporaryDirectory(prefix="recordian-rust-gui-") as directory:
            scratch = Path(directory)
            for name in ("config", "data", "cache", "run"):
                (scratch / name).mkdir(mode=0o700)
            portal = scratch / "config" / "xdg-desktop-portal"
            portal.mkdir()
            (portal / "portals.conf").write_text("[preferred]\ndefault=none\n")
            env = {
                **os.environ,
                "DISPLAY": display,
                "GDK_BACKEND": "x11",
                "GTK_IM_MODULE": "fcitx",
                "XMODIFIERS": "@im=fcitx",
                "QT_IM_MODULE": "fcitx",
                "XDG_RUNTIME_DIR": str(scratch / "run"),
                "XDG_CONFIG_HOME": str(scratch / "config"),
                "XDG_DATA_HOME": str(scratch / "data"),
                "XDG_CACHE_HOME": str(scratch / "cache"),
                "RECORDIAN_NATIVE_TMP": directory,
                "RECORDIAN_NATIVE_BUILD": str(build),
                "RECORDIAN_CANDIDATE_SRC": str(root / "src"),
                "RECORDIAN_NATIVE_PY": _find_gtk_python(),
                "RECORDIAN_NATIVE_CORE": "required",
                "PYTHONPATH": str(root / "src"),
                "XDG_SESSION_TYPE": "x11",
                "NO_AT_BRIDGE": "1",
            }
            for name in (
                "WAYLAND_DISPLAY",
                "WAYLAND_SOCKET",
                "HYPRLAND_INSTANCE_SIGNATURE",
                "SWAYSOCK",
                "SESSION_MANAGER",
                "DBUS_SESSION_BUS_ADDRESS",
            ):
                env.pop(name, None)
            result = subprocess.run(
                ["dbus-run-session", "--", sys.executable, str(root / "tests/native/drive_rust_runtime.py")],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout + "\n" + result.stderr
            log = Path(os.environ.get("RECORDIAN_RUST_GUI_LOG", str(build / "rust-runtime-validation.log")))
            log.write_text(output, encoding="utf-8")
            assert result.returncode == 0, f"Rust GTK acceptance failed; {log}\n{output[-5000:]}"
            assert "RUST_RUNTIME PASS" in result.stdout
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait(timeout=5)
