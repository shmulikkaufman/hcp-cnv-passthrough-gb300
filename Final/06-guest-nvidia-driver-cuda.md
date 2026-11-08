# 6 — In-guest NVIDIA driver + CUDA validation

Install the NVIDIA driver and CUDA inside each guest and validate the
passed-through GPUs. This is the acceptance gate for GPU passthrough.
Tooling: `configs/05-guest-driver/install-nvidia-driver.sh` (pinned install),
`configs/06-cuda-test/` (probe + samples).

## 6.1 Register the guest and enable repos

Each VM registers with **its own** RHSM identity (activation key supplied
at deploy time — never stored or committed):

```bash
sudo subscription-manager register --org=<org> --activationkey=<key>
sudo subscription-manager repos --enable=rhel-10-for-aarch64-nvidia-rpms
sudo subscription-manager repos --enable=codeready-builder-for-rhel-10-aarch64-rpms
sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm
sudo dnf config-manager --add-repo \
  https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/cuda-rhel10.repo
```

## 6.2 Install the driver, pinned to the running kernel

Version pinning matters: kmod, userspace **and kernel-devel must match the
running kernel exactly**, otherwise dnf pulls a newer `kernel-core` and
DKMS builds for the wrong kernel. The validated combination is driver
**615.71.10** on kernel `6.12.0-259.26.el10nv` (the script
`configs/05-guest-driver/install-nvidia-driver.sh` encodes this):

```bash
DRIVER_VERSION=615.71.10
KERNEL_VERSION=$(uname -r | sed 's/+64k//')

sudo dnf install -y \
  kernel-64k-devel-${KERNEL_VERSION} \
  kernel-devel-matched-${KERNEL_VERSION} \
  kmod-nvidia-open-dkms-${DRIVER_VERSION} \
  nvidia-driver-${DRIVER_VERSION} \
  nvidia-modprobe-${DRIVER_VERSION}

sudo dnf install -y nvidia-driver-cuda-${DRIVER_VERSION}   # ships nvidia-smi

sudo reboot    # guest-internal reboot only; DKMS module loads on fresh boot
```

The reboot is guest-internal: the VMI keeps its UID and stays Running
throughout. Do not use `modprobe -r nvidia` to reload after any failed
probe — the unload hangs; reboot the guest (Appendix A).

## 6.3 Post-boot verification

```bash
sudo dkms status      # nvidia/615.71.10, 6.12.0-259.26.el10nv.aarch64: installed
nvidia-smi            # 2x GB300, driver 615.71.10, CUDA UMD 13.4
sudo dmesg | grep NVRM
```

A healthy boot logs the NVRM load line with **none** of the historical
failure signatures (`kmemsysSetupCoherentCpuLink`, `kbusVerifyBar2`,
`RmInitAdapter failed` — those belong to the retired 4.21 workarounds;
see Appendix A). DVSEC3 PDI, `kbifCacheVFInfo` and FLA-sysmem lines are
informational in a VM. If any NIC VFs are already attached (Chapter 7),
verify they survived the reboot with RDMA ports ACTIVE.

## 6.4 CUDA validation

Three levels, cheapest first:

**1. Driver-API probe** (no toolkit needed — uses `libcuda.so.1`):

```bash
python3 configs/06-cuda-test/cuda-driver-probe.py
```

Expected on a healthy VM: `cuInit: 0 CUDA_SUCCESS`, count: 2, context
create, 256 MiB alloc/free, PASS. This is the fastest regression check.

**2. Quick samples:**

```bash
sudo dnf install -y cuda-toolkit-13-4 git cmake
git clone https://github.com/NVIDIA/cuda-samples && cd cuda-samples
export PATH=/usr/local/cuda/bin:$PATH
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_ARCHITECTURES=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
cmake --build build --target deviceQuery vectorAdd -j$(nproc)
./build/cpp/1_Utilities/deviceQuery/deviceQuery    # Result = PASS
./build/cpp/0_Introduction/vectorAdd/vectorAdd     # Test PASSED
```

The GB300 reports compute capability **10.3** (`sm_103`). Headless VMs
skip the GL/GLUT/Vulkan/MPI/NvSCI samples at configure time (expected);
`cdp*` samples skip because they only list `87;110` as supported
architectures.

**3. Full sample suite + soak (optional but performed):**

```bash
cmake --build build -j$(nproc) && cmake --install build
python3 run_tests.py --dir ./build/bin/aarch64/linux/release --output res --parallel 2
```

Validated result: **115/118 pass, 2 waived** (single→dual GPU P2P samples
self-waive by design on 1-GPU VMs; with 2 GPUs they pass), **1 fail**
(`ptxgen` — a known libNVVM sample issue that fails identically in NVIDIA
QE's own GB300 run on this stack; not a passthrough defect).

Additional validated evidence on this profile (2026-11-01, 2-GPU VM):
`nvbandwidth` — H2D 211 GB/s / D2H 193 GB/s via the coherent C2C path,
local SM read 7.6 TB/s, **GPU↔GPU 770–776 GB/s over hardware NVLink**,
1.75 µs peer latency; `gpu-burn` 120 s soak on both GPUs — zero ECC
errors, zero thermal/power violations. ECC is enabled on both GPUs.

Both active VMs meet this standard (VM3 validated 2026-11-05 with CUDA
13.5 toolkit, VM12 with 13.4).
