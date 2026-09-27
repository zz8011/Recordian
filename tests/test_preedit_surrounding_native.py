"""Compile and exercise the C++ bridge's real surrounding-text policy."""
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


def test_native_preedit_surrounding_policy(tmp_path):
    if not shutil.which("g++") or not shutil.which("pkg-config"):
        pytest.skip("C++ compiler and Fcitx development files required")
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "Fcitx5Core"],
        capture_output=True, text=True,
    )
    if flags.returncode:
        pytest.skip("Fcitx development files required")
    source = Path(__file__).parent / "native" / "preedit_surrounding_test.cpp"
    executable = tmp_path / "preedit-surrounding-test"
    subprocess.run(
        ["g++", "-std=c++20", str(source), "-o", str(executable),
         *shlex.split(flags.stdout)], check=True, capture_output=True, timeout=60,
    )
    subprocess.run([str(executable)], check=True, timeout=5)
