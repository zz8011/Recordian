#!/bin/bash
# Runs inside the download container. Paths are the mounted /work tree.
set -eu
REV=49564ddcfccafb6db563eb757c1d41e6c78dcb56
EXPECT=8411558400
PY=/work/venv/bin/python
date -u +%Y-%m-%dT%H:%M:%SZ
fetch_tree() {
  url="$1/api/models/Mapika/decider-4b/tree/${REV}?recursive=1"
  echo "TREE $url"
  curl -fsSL --retry 3 --max-time 90 "$url" -o /work/logs/hf-tree.json
}
if fetch_tree https://hf-mirror.com; then
  echo ENDPOINT=https://hf-mirror.com > /work/logs/hf-endpoint
else
  echo MIRROR_TREE_FAIL
  fetch_tree https://huggingface.co
  echo ENDPOINT=https://huggingface.co > /work/logs/hf-endpoint
fi
"$PY" - << 'PY'
import json
rows = json.load(open("/work/logs/hf-tree.json"))
if isinstance(rows, dict):
    rows = rows.get("siblings") or rows.get("tree") or []
hit = None
for row in rows:
    if row.get("path") == "model.safetensors" or row.get("rfilename") == "model.safetensors":
        hit = row
        break
if not hit:
    raise SystemExit("model.safetensors missing from tree")
lfs = hit.get("lfs") or {}
# Hub tree JSON names the LFS sha256 "oid". The outer "oid" is the git blob id.
sha = lfs.get("sha256") or lfs.get("oid") or ""
size = int(hit.get("size") or lfs.get("size") or 0)
open("/work/logs/hf-lfs.txt", "w").write(f"{sha} {size}\n")
print("LFS", sha, size)
if size != 8411558400:
    raise SystemExit(f"size {size} != 8411558400")
if len(sha) != 64:
    raise SystemExit("missing 64-char lfs sha256")
PY
EP=$(cut -d= -f2 /work/logs/hf-endpoint)
export HF_ENDPOINT="$EP"
export HF_HUB_DISABLE_TELEMETRY=1
export HF_HUB_ENABLE_HF_TRANSFER=0
echo "USING $HF_ENDPOINT"
"$PY" - << 'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="Mapika/decider-4b",
    revision="49564ddcfccafb6db563eb757c1d41e6c78dcb56",
    local_dir="/work/weights/decider-4b",
    endpoint=os.environ["HF_ENDPOINT"],
)
print("DOWNLOAD_RETURNED")
PY
sha256sum /work/weights/decider-4b/model.safetensors | tee /work/logs/model.sha256
stat -c %s /work/weights/decider-4b/model.safetensors | tee /work/logs/model.bytes
"$PY" - << 'PY'
import json
import pathlib
root = pathlib.Path("/work/weights/decider-4b")
need = ["model.safetensors", "config.json", "decider_config.json", "tokenizer.json", "tokenizer_config.json"]
missing = [n for n in need if not (root / n).is_file()]
if missing:
    raise SystemExit("missing " + ",".join(missing))
cfg = json.loads((root / "decider_config.json").read_text())
print("DECIDER_CONFIG", {k: cfg.get(k) for k in ("version", "temperature", "layout", "schema_first")})
if cfg.get("version") != "4b-v2" or float(cfg.get("temperature")) != 1.935 or cfg.get("layout") != "plain" or bool(cfg.get("schema_first")):
    raise SystemExit("decider_config pin mismatch")
got = open("/work/logs/model.sha256").read().split()[0]
exp = open("/work/logs/hf-lfs.txt").read().split()[0]
size = int(open("/work/logs/model.bytes").read().strip())
print("SHA_OK", got == exp, "BYTES", size)
if got != exp or size != 8411558400:
    raise SystemExit("hash or size mismatch")
print("WEIGHTS_OK")
PY
echo 0 > /work/logs/download.exit
echo DOWNLOAD_INNER_OK
