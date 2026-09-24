#!/usr/bin/env python3
"""Pin check for the real bf16 weights before the trial container may start.

Every start verifies the *whole* file: the exact byte size and the full sha256 of the 8.4 GB
``model.safetensors`` must equal the pinned values.  There is no chunk sampling, no inode/mtime
shortcut and no cached attestation, because none of those prove that the file as a whole is
still the pinned file -- they only prove that parts of a previously seen file survived.  A full
read of 8.4 GB takes a few tens of seconds and happens before ``docker run``, so no request
ever waits for it.

Usage:
  weights-pin-check.py [model.safetensors]   exit 0 only when the file is the pin, else 5
  weights-pin-check.py --help
"""

import hashlib
import os
import sys
import time

EXPECTED_SHA256 = "69e6895461c425c6469cd304838a2e5673141613c2da04782026e37c1481d936"
EXPECTED_SIZE = 8411558400
MODEL = "/media/v/Data/recordian-decider-trial/weights/decider-4b/model.safetensors"
BLOCK = 8 * 1024 * 1024


def sha256_of(path):
    """sha256 of the whole file, streamed in 8 MiB blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(BLOCK)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def fail(msg):
    print(f"WEIGHTS_PIN FAIL {msg}")
    return 5


def check(path):
    if not os.path.isfile(path):
        return fail(f"missing file {path}")
    size = os.stat(path).st_size
    if size != EXPECTED_SIZE:
        return fail(f"size {size} != pinned {EXPECTED_SIZE}")
    t0 = time.perf_counter()
    digest = sha256_of(path)
    ms = (time.perf_counter() - t0) * 1000
    if digest != EXPECTED_SHA256:
        return fail(f"full sha256 {digest} != pinned {EXPECTED_SHA256}")
    print(f"WEIGHTS_PIN OK mode=full-sha256 path={path} size={size} sha256={digest} ms={ms:.0f}")
    return 0


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args and args[0].startswith("-"):
        print(__doc__)
        return 2
    return check(args[0] if args else MODEL)


if __name__ == "__main__":
    raise SystemExit(main())
