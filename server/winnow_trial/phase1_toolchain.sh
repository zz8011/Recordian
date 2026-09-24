#!/bin/bash
# Host-side orchestrator. Run on 192.168.5.111. Installs build packages only
# inside recordian-winnow-trial-toolchain, then configures the pinned HIP build.
set -u
ROOT=/media/v/Data/recordian-winnow-trial
LOG=$ROOT/logs
SRC=$ROOT/src/winnow-inference
IMAGE=kyuz0/amd-strix-halo-toolboxes:rocm-10.0
NAME=recordian-winnow-trial-toolchain
WINNOW_COMMIT=77d14580c6732ca2f3745750c1dc1fd446d8bcee
mkdir -p "$LOG" "$ROOT/src" "$ROOT/models/gguf" "$ROOT/build" /home/v/services/recordian-winnow-trial
exec >> "$LOG/phase1.log" 2>&1
set -euo pipefail
echo "UTC_START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "state toolchain" > "$LOG/state.txt"
free -b | awk 'NR==2 {print "mem_available", $7}'

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "container $NAME already exists; not touching other containers"
  docker ps -a --filter "name=^/${NAME}$" --format '{{.ID}} {{.Status}}'
else
  docker run -d --name "$NAME" \
    --runtime amd \
    --memory=28g --memory-swap=28g \
    --device /dev/kfd --device /dev/dri \
    --group-add 992 --group-add 44 \
    -e HIP_VISIBLE_DEVICES=0 \
    -e AMD_VISIBLE_DEVICES=0 \
    -e HSA_NO_SCRATCH_RECLAIM=1 \
    -v "$ROOT:/work" \
    --entrypoint bash \
    "$IMAGE" \
    -lc 'sleep infinity'
fi
echo "container_id $(docker inspect -f '{{.Id}}' "$NAME")"

docker exec "$NAME" bash -lc '
set -eu
# rocm.repo stays as the base image shipped it (gpgcheck=1, AMD packages.gpg).
if [ -f /etc/yum.repos.d/rocm.repo ]; then
  grep -q "^gpgcheck=1" /etc/yum.repos.d/rocm.repo
  grep -q "packages.gpg" /etc/yum.repos.d/rocm.repo
fi
if ! command -v cmake >/dev/null || ! rpm -q gcc-c++ >/dev/null 2>&1; then
  key=/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64
  primary=/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary
  test -f "$key"
  test -f "$primary"
  # Same path the image fedora.repo uses: $releasever-$basearch -> fedora-44-primary.
  test "$(readlink -f "$key")" = "$(readlink -f "$primary")"
  cat > /etc/yum.repos.d/fedora-signed.repo << EOF
[fedora-signed]
name=Fedora 44 - signed
baseurl=https://mirrors.tuna.tsinghua.edu.cn/fedora/releases/44/Everything/x86_64/os/
enabled=1
repo_gpgcheck=0
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64
[updates-signed]
name=Fedora 44 updates - signed
baseurl=https://mirrors.tuna.tsinghua.edu.cn/fedora/updates/44/Everything/x86_64/
enabled=1
repo_gpgcheck=0
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64
EOF
  # Command-scoped. Does not rewrite rocm.repo. Stock fedora.repo stays gpgcheck=1 on disk.
  dnf install -y --setopt=timeout=40 --setopt=retries=2 \
    --disablerepo=fedora --disablerepo=updates --disablerepo=fedora-cisco-openh264 \
    --disablerepo=fedora-updates-testing \
    --enablerepo=fedora-signed --enablerepo=updates-signed \
    cmake ninja-build git gcc gcc-c++ openssl-devel make python3 pkgconf-pkg-config which
fi
echo CMAKE $(cmake --version | head -n 1)
echo GCC $(gcc --version | head -n 1)
echo GXX $(g++ --version | head -n 1)
echo GIT $(git --version)
test -f /usr/include/openssl/ssl.h
test -f /usr/include/c++/*/cmath || ls /usr/include/c++/*/cmath
'

echo "state hip-smoke" > "$LOG/state.txt"
docker exec "$NAME" bash -lc '
set -eu
cat > /tmp/tiny.hip << "EOF"
#include <hip/hip_runtime.h>
#include <cstdio>
__global__ void k(int* x) { *x = 42; }
int main() {
  int dev = 0;
  hipError_t e = hipGetDeviceCount(&dev);
  std::printf("count_err %d count %d\n", (int)e, dev);
  if (e != hipSuccess || dev < 1) return 2;
  hipDeviceProp_t p{};
  hipGetDeviceProperties(&p, 0);
  std::printf("name %s gcn %s\n", p.name, p.gcnArchName);
  int* d = nullptr;
  e = hipMalloc(&d, sizeof(int));
  std::printf("malloc %d\n", (int)e);
  if (e != hipSuccess) return 3;
  k<<<1,1>>>(d);
  e = hipDeviceSynchronize();
  std::printf("sync %d\n", (int)e);
  int h = 0;
  hipMemcpy(&h, d, sizeof(int), hipMemcpyDeviceToHost);
  hipFree(d);
  std::printf("value %d\n", h);
  return h == 42 ? 0 : 4;
}
EOF
hipcc --offload-arch=gfx1151 -O2 -o /tmp/tiny /tmp/tiny.hip
/tmp/tiny
'

echo "state clone" > "$LOG/state.txt"
docker exec "$NAME" bash -lc "
set -eu
if [ ! -d /work/src/winnow-inference/.git ]; then
  git clone --filter=blob:none https://github.com/EldanRing/winnow-inference.git /work/src/winnow-inference \
    || git clone --filter=blob:none https://ghfast.top/https://github.com/EldanRing/winnow-inference.git /work/src/winnow-inference
fi
git -C /work/src/winnow-inference checkout --detach $WINNOW_COMMIT
git -C /work/src/winnow-inference rev-parse HEAD
test -f /work/tools/build_hip.py
"

echo "state configure" > "$LOG/state.txt"
docker exec "$NAME" python3 /work/tools/build_hip.py \
  --root /work/src/winnow-inference \
  --build-dir /work/build \
  --jobs 4 \
  --configure
echo "UTC_CONFIGURE_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "state configure-ok" > "$LOG/state.txt"
docker commit "$NAME" recordian-winnow-trial:toolchain >/dev/null
echo "image recordian-winnow-trial:toolchain"
echo "UTC_PHASE1_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
