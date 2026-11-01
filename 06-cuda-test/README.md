# CUDA validation inside the GPU VM

Validated 2026-11-01 on `gpu-vm-rhel-validation` (RHEL 10.2 ARM64, 64 KiB
pages, NVIDIA driver 615.71.10, GB300 passed through via CNV 4.22 AIE).

Two levels of validation:

1. **Driver-API probe** — no CUDA toolkit needed, uses `libcuda.so.1`
   directly (ships with `nvidia-driver-cuda`). Proves driver init, device
   enumeration, context creation and device-memory alloc/free.
2. **cuda-samples** — full compile-and-run of NVIDIA's sample suite
   (`deviceQuery`, `vectorAdd`, …) for the GB300's compute capability.

## Prerequisites (inside the VM)

Done in Phase 5 of the top-level README: driver RPMs installed and the VM
rebooted with **no** legacy workarounds (no MMIO hook on the VM spec, no
`RMForceBarPath` in `/etc/modprobe.d/`).

Sanity check first:

```bash
nvidia-smi    # must show the GB300 before anything here will work
```

## 1. Driver-API probe (no toolkit required)

```bash
python3 cuda-driver-probe.py
```

Expected output on a healthy VM:

```
cuInit: 0 CUDA_SUCCESS
cuDeviceGetCount: 0 count: 1
Device 0: NVIDIA GB300 total mem: 283301 MiB
cuCtxCreate: 0
cuMemAlloc(256MiB): 0
cuMemFree: 0
```

Before the CNV 4.22 fix this same probe returned `cuInit: 999
CUDA_ERROR_UNKNOWN` — it is the quickest before/after regression check.

## 2. cuda-samples

### 2.1 Install the toolchain

```bash
sudo dnf install -y cuda-toolkit-13-4 git cmake
```

(`cuda-toolkit-13-4` matches the driver's CUDA UMD version 13.4; the
`cuda-rhel10-sbsa` repo was already added in Phase 5. `make`/`g++` are
already present from the DKMS driver build.)

### 2.2 Clone and build

```bash
git clone https://github.com/NVIDIA/cuda-samples
cd cuda-samples

export PATH=/usr/local/cuda/bin:$PATH
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_ARCHITECTURES=$(nvidia-smi --query-gpu=compute_cap \
        --format=csv,noheader | head -1 | tr -d '.')
cmake --build build --target deviceQuery vectorAdd -j$(nproc)
```

The GB300 reports compute capability **10.3** → binaries are built for
`sm_103`. Pinning the architecture avoids compiling ~20 architectures and is
required for a quick build; drop `-DCMAKE_CUDA_ARCHITECTURES=...` to build
for all supported archs (much slower).

Headless-server notes (expected, not errors):

- OpenGL/GLUT/Vulkan/MPI/NvSCI samples are skipped (no display stack).
- The `cdp*` dynamic-parallelism samples are skipped because they only list
  `87;110` as supported architectures — irrelevant for validation.

### 2.3 Run the quick tests

```bash
./build/cpp/1_Utilities/deviceQuery/deviceQuery   # must end: Result = PASS
./build/cpp/0_Introduction/vectorAdd/vectorAdd    # must print: Test PASSED
```

`deviceQuery` should report: `NVIDIA GB300`, CUDA 13.4 / 13.4, capability
10.3, ~283 GiB global memory.

### 2.4 (Optional) full sample suite

```bash
cmake --build build -j$(nproc)     # build everything (~160 samples)
cmake --install build              # populates build/bin/aarch64/linux/release
python3 run_tests.py --dir ./build/bin/aarch64/linux/release \
    --output res --parallel 2
```

Expected: **115/118 pass, 2 waived, 1 fail** (measured 2026-11-01). The two
waived samples (`streamOrderedAllocationP2P`, `conjugateGradientMultiDeviceCG`)
self-waive on a single-GPU VM by design. The one failure, `ptxgen` (exit 16),
is **identical to NVIDIA QE's own GB300 run on this same stack** (internal
NVIDIA-1408, Oct 2026) — a libNVVM sample issue, not a passthrough defect.
GL/GLUT/Vulkan/MPI samples are skipped at build time on this headless VM,
which is why 118 executables run here versus 163 in QE's run (their image
built the GL samples, which then failed at runtime — 26 failures there
versus 1 here).

## Files

| File | Purpose |
|---|---|
| `cuda-driver-probe.py` | ctypes probe of the CUDA driver API (no toolkit needed) |
| `build-samples.sh` | clone + configure + build `deviceQuery`/`vectorAdd` |
| `run-samples.sh` | run the quick tests and print PASS/FAIL summary |
