#!/usr/bin/env python3
"""CUDA driver-API probe for a passed-through NVIDIA GPU.

Calls libcuda.so.1 directly via ctypes — no CUDA toolkit / nvcc required
(libcuda ships with the nvidia-driver-cuda RPM). Verifies, in order:

  cuInit            driver <-> GPU communication end-to-end
  cuDeviceGetCount  device enumeration
  cuDeviceGetName   device identity
  cuDeviceTotalMem  HBM size
  cuCtxCreate       execution context
  cuMemAlloc        device-memory allocation
  cuMemFree         release

On the CNV 4.22 GB300 stack this is the quickest regression check: before
the hook/RMForceBarPath fix it returned `cuInit: 999 CUDA_ERROR_UNKNOWN`.
"""

import sys
import ctypes


def main() -> int:
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError as e:
        print(f"FAIL: cannot load libcuda.so.1: {e}")
        return 1

    def err_name(code: int) -> str:
        s = ctypes.c_char_p()
        if cuda.cuGetErrorName(code, ctypes.byref(s)) == 0 and s.value:
            return s.value.decode()
        return "unknown"

    rc = cuda.cuInit(0)
    print(f"cuInit: {rc} {err_name(rc)}")
    if rc != 0:
        return 2

    count = ctypes.c_int(-1)
    rc = cuda.cuDeviceGetCount(ctypes.byref(count))
    print(f"cuDeviceGetCount: {rc} count: {count.value}")
    if rc != 0 or count.value < 1:
        return 3

    dev = ctypes.c_void_p()
    cuda.cuDeviceGet(ctypes.byref(dev), 0)
    name = ctypes.create_string_buffer(256)
    cuda.cuDeviceGetName(name, 256, dev)
    total = ctypes.c_size_t()
    cuda.cuDeviceTotalMem_v2(ctypes.byref(total), dev)
    print(f"Device 0: {name.value.decode()} total mem: "
          f"{total.value // (1024 * 1024)} MiB")

    ctx = ctypes.c_void_p()
    rc = cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev)
    print(f"cuCtxCreate: {rc}")
    if rc != 0:
        return 4

    nbytes = 256 * 1024 * 1024
    dptr = ctypes.c_void_p()
    rc = cuda.cuMemAlloc_v2(ctypes.byref(dptr), ctypes.c_size_t(nbytes))
    print(f"cuMemAlloc({nbytes // (1024 * 1024)}MiB): {rc} "
          f"ptr: {hex(dptr.value or 0)}")
    if rc != 0:
        return 5

    rc = cuda.cuMemFree_v2(dptr)
    print(f"cuMemFree: {rc}")
    cuda.cuCtxDestroy_v2(ctx)
    print("PASS: driver API functional")
    return 0


if __name__ == "__main__":
    sys.exit(main())
