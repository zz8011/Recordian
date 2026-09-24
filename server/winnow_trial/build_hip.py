#!/usr/bin/env python3
"""Prepare the pinned Winnow/llama.cpp tree and configure a HIP build.

Source preparation matches scripts/build.py at
77d14580c6732ca2f3745750c1dc1fd446d8bcee: clone the locked llama.cpp
commit, verify patch hashes, apply those patches, and reject any other
source drift. The CUDA/Metal flags from upstream are replaced by GGML_HIP
for gfx1151. This does not change decision scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

LOCK_COMMIT = "911f6cdc8ab8a530b2bee09ee61471a6f3178eeb"


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def prepare(root: Path) -> None:
    lock = json.loads((root / "runtime.lock.json").read_text())
    if lock["commit"] != LOCK_COMMIT:
        raise SystemExit(f"runtime.lock.json commit is {lock['commit']}, expected {LOCK_COMMIT}")
    source = root / ".runtime/llama.cpp"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", lock["repository"], str(source)],
            root,
        )
        run(["git", "-C", str(source), "checkout", "--detach", lock["commit"]], root)
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != lock["commit"]:
        raise SystemExit(f"unexpected runtime revision {revision}")
    for item in lock["patches"]:
        patch = root / item["file"]
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise SystemExit(f"patch hash mismatch: {patch.name}")
        check = subprocess.run(
            ["git", "-C", str(source), "apply", "--check", str(patch)],
            capture_output=True,
        )
        if check.returncode == 0:
            run(["git", "-C", str(source), "apply", str(patch)], root)
        elif subprocess.run(
            ["git", "-C", str(source), "apply", "--reverse", "--check", str(patch)],
            capture_output=True,
        ).returncode:
            raise SystemExit(f"cannot apply patch: {patch.name}")
    changed = subprocess.check_output(
        ["git", "-C", str(source), "diff", "HEAD", "--name-only"], text=True
    ).splitlines()
    if set(changed) != set(lock["source_sha256"]) or any(
        hashlib.sha256((source / name).read_bytes()).hexdigest() != digest
        for name, digest in lock["source_sha256"].items()
    ):
        raise SystemExit("runtime contains changes outside its verified lock")
    print("runtime lock ok", revision, flush=True)


def configure(root: Path, build_dir: Path, jobs: int) -> None:
    hipcc = "/opt/rocm/bin/hipcc"
    clang = "/opt/rocm/llvm/bin/clang"
    build_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "cmake",
        "-S",
        str(root),
        "-B",
        str(build_dir),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_C_COMPILER={clang}",
        f"-DCMAKE_CXX_COMPILER={hipcc}",
        "-DGGML_CUDA=OFF",
        "-DGGML_METAL=OFF",
        "-DGGML_HIP=ON",
        "-DGGML_HIP_RCCL=OFF",
        "-DGGML_HIP_NO_VMM=ON",
        "-DGGML_HIP_MMQ_MFMA=OFF",
        "-DGGML_STATIC=OFF",
        "-DAMDGPU_TARGETS=gfx1151",
        "-DGPU_TARGETS=gfx1151",
        "-DCMAKE_HIP_ARCHITECTURES=gfx1151",
    ]
    run(cmd, root)
    print("configure ok", build_dir, "jobs", jobs, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--configure", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1 or args.jobs > 6:
        raise SystemExit("jobs must be 1..6")
    prepare(args.root.resolve())
    if args.configure:
        configure(args.root.resolve(), args.build_dir.resolve(), args.jobs)


if __name__ == "__main__":
    main()
