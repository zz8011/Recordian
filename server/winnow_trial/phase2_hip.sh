#!/bin/bash
# Second fix: install the image's own ROCm 10 devel headers, then HIP-smoke,
# then configure the pinned tree. Does not download model weights.
set -u
ROOT=/media/v/Data/recordian-winnow-trial
LOG=$ROOT/logs
NAME=recordian-winnow-trial-toolchain
mkdir -p "$LOG"
exec >> "$LOG/phase2.log" 2>&1
set -euo pipefail
echo "UTC_START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "state devel-install" > "$LOG/state.txt"
docker exec "$NAME" bash -lc '
set -eu
# Use the image rocm.repo unchanged (gpgcheck=1, gpgkey=packages.gpg). Do not rewrite it.
test -f /etc/yum.repos.d/rocm.repo
grep -q "^gpgcheck=1" /etc/yum.repos.d/rocm.repo
dnf install -y --disablerepo="*" --enablerepo=amdrocm-stable \
  amdrocm-runtime-devel10.0 \
  amdrocm-blas-devel10.0 \
  amdrocm-hipblas-common-devel10.0 \
  amdrocm-core-devel10.0-gfx1151
test -f /opt/rocm/include/hip/hip_runtime.h || find /opt/rocm -name hip_runtime.h | head
'
echo "state hip-smoke-2" > "$LOG/state.txt"
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
docker exec "$NAME" bash -lc '
set -eu
if [ ! -d /work/src/winnow-inference/.git ]; then
  git clone --filter=blob:none https://github.com/EldanRing/winnow-inference.git /work/src/winnow-inference \
    || git clone --filter=blob:none https://ghfast.top/https://github.com/EldanRing/winnow-inference.git /work/src/winnow-inference
fi
git -C /work/src/winnow-inference checkout --detach 77d14580c6732ca2f3745750c1dc1fd446d8bcee
git -C /work/src/winnow-inference rev-parse HEAD
test -f /work/tools/build_hip.py
'
echo "state configure" > "$LOG/state.txt"
docker exec "$NAME" python3 /work/tools/build_hip.py \
  --root /work/src/winnow-inference \
  --build-dir /work/build \
  --jobs 4 \
  --configure
echo "UTC_CONFIGURE_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "state configure-ok" > "$LOG/state.txt"
docker commit "$NAME" recordian-winnow-trial:toolchain >/dev/null
echo "UTC_PHASE2_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
