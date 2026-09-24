#!/bin/bash
# Resume the pinned Q8 GGUF until size and SHA256 match the Hub release.
set -u
out=/media/v/Data/recordian-winnow-trial/models/gguf/Winnow-12B-Q8_0.gguf
part=$out.partial
log=/media/v/Data/recordian-winnow-trial/logs/download.log
url=https://hf-mirror.com/EldanRing/Winnow-12B/resolve/b6ac22b0d51b69b18200acacb3fbdd98073fffe8/gguf/Winnow-12B-Q8_0.gguf
expect=12669646592
sha=b710efc4c0d048ee61eed92c5fef5ce323a4d17e7c51f9f0533cc72ae50818ea
receipt=$out.sha256receipt
mkdir -p "$(dirname "$out")"
receipt_ok() {
  [ -f "$receipt" ] || return 1
  rsha=$(awk -F= '$1=="sha256" {print $2}' "$receipt")
  rsz=$(awk -F= '$1=="size" {print $2}' "$receipt")
  rpath=$(awk -F= '$1=="path" {print $2}' "$receipt")
  [ "$rsha" = "$sha" ] && [ "$rsz" = "$expect" ] && [ "$rpath" = "$out" ]
}
echo "UTC_LOOP_START $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$log"
for i in $(seq 1 50); do
  sz=0
  if [ -f "$out" ]; then sz=$(stat -c %s "$out"); fi
  echo "try $i size $sz" >> "$log"
  if [ -f "$out" ] && [ "$sz" = "$expect" ] && receipt_ok; then
    echo ALREADY_FINAL >> "$log"
    exit 0
  fi
  if [ -f "$out" ] && [ "$sz" = "$expect" ]; then
    break
  fi
  curl -L --retry 2 --retry-delay 2 -C - -o "$part" "$url" || echo "curl_nonzero try $i" >> "$log"
  if [ -f "$part" ]; then
    psz=$(stat -c %s "$part")
    if [ "$psz" = "$expect" ]; then
      break
    fi
  fi
  sleep 2
done
target=$part
if [ -f "$out" ] && [ ! -f "$part" ]; then
  target=$out
fi
sz=$(stat -c %s "$target")
echo SIZE "$sz" >> "$log"
if [ "$sz" != "$expect" ]; then
  echo SIZE_MISMATCH >> "$log"
  exit 2
fi
echo "$sha  $target" | sha256sum -c - >> "$log" 2>&1 || { echo SHA_MISMATCH >> "$log"; exit 3; }
if [ "$target" = "$part" ]; then
  mv -f "$part" "$out"
fi
printf 'sha256=%s\nsize=%s\npath=%s\n' "$sha" "$expect" "$out" > "$receipt"
echo "UTC_DL_OK $(date -u +%Y-%m-%dT%H:%M:%SZ) SOURCE hf-mirror.com->huggingface cdn" >> "$log"
