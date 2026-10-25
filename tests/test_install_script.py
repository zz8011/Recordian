"""Tests for install.sh script logic."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_install_script_help() -> None:
    """install.sh supports -h / --help without error."""
    res = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "用法:" in res.stdout


def test_install_script_upgrades_isolated_venv(tmp_path: Path) -> None:
    """Running install.sh on existing isolated venv enables system-site-packages."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_home = workspace / "home"
    fake_home.mkdir()

    # Copy actual install.sh into workspace
    shutil.copy(REPO_ROOT / "install.sh", workspace / "install.sh")

    # Pre-create isolated venv with include-system-site-packages = false
    subprocess.run(
        [sys.executable, "-m", "venv", str(workspace / ".venv")],
        check=True,
    )

    pyvenv_cfg = workspace / ".venv" / "pyvenv.cfg"
    assert "include-system-site-packages = false" in pyvenv_cfg.read_text(encoding="utf-8")

    # Intercept pip in venv to prevent network/model operations during test
    fake_pip = workspace / ".venv" / "bin" / "pip"
    fake_pip.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pip.chmod(0o755)

    # Provide fake system binaries (xclip, etc.) so dependency prompt does not block
    fake_bin = workspace / "bin"
    fake_bin.mkdir()
    for bin_name in ("xclip", "xdotool", "notify-send"):
        p = fake_bin / bin_name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHON"] = sys.executable

    res = subprocess.run(
        ["bash", "./install.sh"],
        cwd=workspace,
        input="y\n",
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"install.sh failed:\n{res.stdout}\n{res.stderr}"

    # Verify real venv configuration was updated to allow system packages
    updated_cfg = pyvenv_cfg.read_text(encoding="utf-8")
    assert "include-system-site-packages = true" in updated_cfg
    assert (workspace / "recordian-launch.sh").is_file()


def test_install_script_refuses_version_mismatch(tmp_path: Path) -> None:
    """install.sh halts without overwriting when existing venv has a different Python version."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copy(REPO_ROOT / "install.sh", workspace / "install.sh")

    # Create dummy venv whose python reports a conflicting version (e.g. 99.99)
    venv_bin = workspace / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_py = venv_bin / "python"
    fake_py.write_text("#!/bin/sh\necho '99.99'\n", encoding="utf-8")
    fake_py.chmod(0o755)

    fake_bin = workspace / "bin"
    fake_bin.mkdir()
    for bin_name in ("xclip", "xdotool", "notify-send"):
        p = fake_bin / bin_name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHON"] = sys.executable

    res = subprocess.run(
        ["bash", "./install.sh"],
        cwd=workspace,
        input="y\n",
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "不一致" in res.stdout or "不匹配" in res.stdout


def test_install_script_rejects_python_below_310(tmp_path: Path) -> None:
    """install.sh rejects explicit PYTHON when version is below 3.10."""
    fake_py = tmp_path / "fake_python38"
    fake_py.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 1; fi\necho "Python 3.8.10"\n', encoding="utf-8")
    fake_py.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHON": str(fake_py)},
    )
    assert res.returncode != 0
    assert "3.10+" in res.stdout or "3.10+" in res.stderr
