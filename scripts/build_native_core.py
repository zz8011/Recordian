#!/usr/bin/env python3
"""Build the dependency-free Linux Rust core and atomically install its ABI1 library.

Uses only Python's standard library. Call during packaging/deployment, never from
the application or an audio callback. Failed builds/ABI checks leave the old library.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "native" / "recordian-core"
LIBRARY = "librecordian_core.so"
METADATA = "librecordian_core.json"
ABI_VERSION = 1


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes() -> dict[str, str]:
    paths = [CRATE / "Cargo.toml", CRATE / "Cargo.lock", *sorted((CRATE / "src").rglob("*.rs"))]
    if not any(path.suffix == ".rs" for path in paths):
        raise RuntimeError("Rust core sources are missing")
    return {path.relative_to(ROOT).as_posix(): sha256(path) for path in paths}


def validate_library(path: Path) -> None:
    """Probe in a fresh process: no cached dlopen handle or crash in the builder."""
    probe = """
import ctypes, sys
lib = ctypes.CDLL(sys.argv[1])
version = lib.recordian_core_abi_version
version.argtypes = []
version.restype = ctypes.c_uint32
if version() != int(sys.argv[2]):
    raise SystemExit("unsupported Recordian native ABI")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(path.resolve()), str(ABI_VERSION)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise RuntimeError("native library cannot load or does not expose ABI1; old library retained")


def build(output_dir: Path | None = None) -> Path:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Recordian native core currently supports Linux only")
    before = source_hashes()
    manifest = str(CRATE / "Cargo.toml")
    # Cargo's structured artifact output also honors CARGO_TARGET_DIR; do not
    # guess a target/release path or accidentally install a stale shared library.
    package_info = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--manifest-path", manifest, "--offline", "--locked", "--no-deps", "--format-version=1"],
        cwd=ROOT, text=True,
    ))
    package = next(p for p in package_info["packages"] if p["name"] == "recordian-core")
    if package["dependencies"]:
        raise RuntimeError("recordian-core must have no Rust dependencies")
    result = subprocess.run(
        ["cargo", "build", "--manifest-path", manifest, "--offline", "--locked", "--release",
         "--message-format=json-render-diagnostics"],
        cwd=ROOT, stdout=subprocess.PIPE, text=True, check=True,
    )
    artifacts: dict[str, Path] = {}
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if message.get("reason") == "compiler-artifact" and message.get("package_id") == package["id"]:
            for filename in message["filenames"]:
                path = Path(filename)
                if path.name in {LIBRARY, "librecordian_core.rlib"}:
                    artifacts[path.name] = path
    if set(artifacts) != {LIBRARY, "librecordian_core.rlib"}:
        raise RuntimeError("release build must produce both cdylib and rlib")
    if before != source_hashes():
        raise RuntimeError("Rust sources changed during the build; retry with a stable checkout")
    version_info = subprocess.check_output(["rustc", "-Vv"], text=True)
    rustc = {}
    for line in version_info.splitlines():
        key, _, value = line.partition(": ")
        if key in {"release", "host", "commit-hash"}:
            rustc[key] = value
    destination = (output_dir or ROOT / "src" / "recordian" / "_native").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # Stage on the same filesystem. Never truncate an inode mapped by a running
    # process. The JSON sidecar is an audit record, not a loader trust assertion.
    with tempfile.TemporaryDirectory(prefix=".recordian-core-", dir=destination) as staging:
        library = Path(staging) / LIBRARY
        shutil.copyfile(artifacts[LIBRARY], library)
        library.chmod(0o755)
        validate_library(library)
        metadata = {
            "schema_version": 1,
            "abi_version": ABI_VERSION,
            "library": LIBRARY,
            "sha256": sha256(library),
            "profile": "release",
            "crate_types": ["cdylib", "rlib"],
            "rustc": rustc,
            "source_sha256": before,
        }
        sidecar = Path(staging) / METADATA
        sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sidecar.chmod(0o644)
        for path in (library, sidecar):
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        # Serialize publication across concurrent builders. Each rename is
        # atomic; readers auditing the pair must compare the recorded SHA256.
        import fcntl

        with (destination / ".build-native.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.replace(library, destination / LIBRARY)
            os.replace(sidecar, destination / METADATA)
            descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return destination / LIBRARY


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="install directory (default: src/recordian/_native)")
    args = parser.parse_args()
    try:
        path = build(args.output_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"native build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Installed ABI{ABI_VERSION}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
