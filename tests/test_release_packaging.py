"""Tests for release packaging and wheel build integrity."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_make_release_in_clean_workspace(tmp_path: Path) -> None:
    """make-release.sh packages native/scripts/presets on clean checkout without leaking secrets/builds."""
    clean_repo = tmp_path / "repo"
    clean_repo.mkdir()

    # Core release scripts and metadata
    for fname in ("make-release.sh", "pyproject.toml", "README.md", "LICENSE", "install.sh", "uninstall.sh", "setup.py", "MANIFEST.in"):
        shutil.copy(REPO_ROOT / fname, clean_repo / fname)

    # Minimal directory fixtures
    for dname in ("assets", "docs", "server", "tests"):
        (clean_repo / dname).mkdir()
        (clean_repo / dname / "placeholder.txt").write_text("ok", encoding="utf-8")

    # Native & scripts without compiling or copying target build cache
    native_core = clean_repo / "native" / "recordian-core" / "src"
    native_core.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "native" / "recordian-core" / "Cargo.toml", clean_repo / "native" / "recordian-core" / "Cargo.toml")
    (native_core / "lib.rs").write_text("// dummy", encoding="utf-8")

    scripts_dir = clean_repo / "scripts"
    scripts_dir.mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "build_native_core.py", scripts_dir / "build_native_core.py")

    # Package source and presets
    pkg_dir = clean_repo / "src" / "recordian"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text('__version__ = "0.1.2"\n', encoding="utf-8")
    shutil.copy(REPO_ROOT / "src" / "recordian" / "preset_manager.py", pkg_dir / "preset_manager.py")
    presets_dir = pkg_dir / "presets"
    presets_dir.mkdir()
    shutil.copy(REPO_ROOT / "src" / "recordian" / "presets" / "default.md", presets_dir / "default.md")
    shutil.copy(REPO_ROOT / "src" / "recordian" / "presets" / "README.md", presets_dir / "README.md")

    # Initialize git repo and stage release tracked files
    subprocess.run(["git", "init"], cwd=clean_repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=clean_repo, check=True, capture_output=True)

    # Plant untracked build artifacts and secrets AFTER staging
    fake_target = clean_repo / "native" / "recordian-core" / "target"
    fake_target.mkdir(parents=True)
    (fake_target / "leak.o").write_text("binary", encoding="utf-8")

    fake_native = pkg_dir / "_native"
    fake_native.mkdir(parents=True)
    (fake_native / "librecordian_core.so").write_text("binary", encoding="utf-8")

    (clean_repo / "server" / ".env.secret").write_text("TOKEN=123", encoding="utf-8")
    (pkg_dir / "private_token.key").write_text("SECRET=456", encoding="utf-8")
    (pkg_dir / "leak.pyc").write_text("bytecode", encoding="utf-8")

    # Clean checkout has no recordian-launch.sh
    assert not (clean_repo / "recordian-launch.sh").exists()

    res = subprocess.run(["bash", "./make-release.sh"], cwd=clean_repo, capture_output=True, text=True)
    assert res.returncode == 0, f"make-release failed:\n{res.stdout}\n{res.stderr}"

    tarballs = list(clean_repo.glob("recordian-*.tar.gz"))
    assert len(tarballs) == 1

    with tarfile.open(tarballs[0], "r:gz") as tf:
        names = tf.getnames()
        prefix = names[0].split("/")[0]

        assert f"{prefix}/native/recordian-core/Cargo.toml" in names
        assert f"{prefix}/scripts/build_native_core.py" in names
        assert f"{prefix}/setup.py" in names
        assert f"{prefix}/MANIFEST.in" in names
        assert f"{prefix}/src/recordian/presets/default.md" in names

        for name in names:
            assert "native/recordian-core/target" not in name, f"Target cache leaked: {name}"
            assert "/_native" not in name and not name.endswith("/_native"), f"Private native build leaked: {name}"
            assert not name.endswith(".secret"), f"Secret leaked: {name}"
            assert not name.endswith(".key"), f"Key leaked: {name}"
            assert not name.endswith(".pyc"), f"Pyc leaked: {name}"


def test_make_release_fails_outside_git_repo(tmp_path: Path) -> None:
    """make-release.sh rejects non-git directory to prevent packaging untracked leaks."""
    non_git = tmp_path / "nongit"
    non_git.mkdir()
    shutil.copy(REPO_ROOT / "make-release.sh", non_git / "make-release.sh")
    shutil.copy(REPO_ROOT / "pyproject.toml", non_git / "pyproject.toml")

    res = subprocess.run(["bash", "./make-release.sh"], cwd=non_git, capture_output=True, text=True)
    assert res.returncode != 0
    assert "git" in res.stdout or "git" in res.stderr


def test_wheel_build_includes_presets(tmp_path: Path) -> None:
    """Build standalone wheel and sdist in temporary copy and verify presets/native sources."""
    if not (importlib.util.find_spec("build") and importlib.util.find_spec("setuptools")):
        pytest.skip("build or setuptools not installed in environment")

    tmp_build = tmp_path / "build_src"
    tmp_build.mkdir()
    shutil.copytree(
        REPO_ROOT / "src",
        tmp_build / "src",
        ignore=shutil.ignore_patterns("_native", "__pycache__", "*.pyc", "*.so", "*.pyd"),
    )
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_build / "pyproject.toml")
    shutil.copy(REPO_ROOT / "setup.py", tmp_build / "setup.py")
    shutil.copy(REPO_ROOT / "MANIFEST.in", tmp_build / "MANIFEST.in")
    shutil.copy(REPO_ROOT / "README.md", tmp_build / "README.md")
    shutil.copy(REPO_ROOT / "LICENSE", tmp_build / "LICENSE")

    native_cargo = tmp_build / "native" / "recordian-core"
    native_cargo.mkdir(parents=True)
    shutil.copytree(
        REPO_ROOT / "native" / "recordian-core",
        native_cargo,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("target"),
    )

    scripts_dir = tmp_build / "scripts"
    scripts_dir.mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "build_native_core.py", scripts_dir / "build_native_core.py")

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    res = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", str(tmp_build), "-o", str(dist_dir)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Build failed:\n{res.stdout}\n{res.stderr}"

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as zf:
        wheel_files = zf.namelist()
        assert "recordian/presets/default.md" in wheel_files
        assert "recordian/presets/README.md" in wheel_files
        assert not any("recordian/_native" in name for name in wheel_files)

    sdists = list(dist_dir.glob("*.tar.gz"))
    assert len(sdists) == 1
    with tarfile.open(sdists[0], "r:gz") as tf:
        sdist_names = tf.getnames()
        prefix = sdist_names[0].split("/")[0]
        assert f"{prefix}/src/recordian/presets/default.md" in sdist_names
        assert f"{prefix}/native/recordian-core/Cargo.toml" in sdist_names
        assert f"{prefix}/native/recordian-core/Cargo.lock" in sdist_names
        assert f"{prefix}/native/recordian-core/src/lib.rs" in sdist_names
        assert not any("recordian/_native" in name for name in sdist_names)
