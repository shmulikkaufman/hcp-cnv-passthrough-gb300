# GPUDirect RDMA (dma-buf) between CNV VMs — validated 2026-11-05

## Summary

**GPU-memory to GPU-memory RDMA works between the two KubeVirt VMs** on the
four-rail CX-8 profile, using the **dma-buf path** (`ibv_reg_dmabuf_mr` +
`cuMemGetHandleForAddressRange`) with the **inbox** `mlx5_core`/`mlx5_ib`
stack — no MOFED, no `nvidia-peermem`. This closes the last untested
intersection of the project goal (RDMA on top of CNV): the same passed-through
GB300 GPUs and CX-8 VFs now carry RDMA writes whose source *and* destination
buffers are device memory, with byte-exact content verification.

Validated on rail `gpu0p0` (VM3 `mlx5_1` / GID 3, 192.0.2.21 ↔ VM12 .22),
re-verified 2026-11-05 with per-test logs persisted:

| Test | Result |
|---|---|
| perftest `ib_write_bw --use_cuda=0 --use_cuda_dmabuf`, 1 QP, 64 KiB × 5000, VM12→VM3 | **386.10 Gb/s** avg (386.34 peak) |
| Same, reverse direction (VM3→VM12) | **386.21 Gb/s** — symmetric |
| Sustained, 16 QPs, 30 s | **390.25 Gb/s** |
| Two GPU-local rails concurrent (gpu0p0+gpu1p0) | **780.02 Gb/s** combined (389.93 + 390.09) |
| Rail counter (`rx_write_requests`) delta | **exactly 5000** per single-QP run (path proof) |
| Mid-run `nvidia-smi` on both VMs | **682 MiB** GPU memory held by `ib_write_bw` |
| Custom content-integrity test (below) | **PASS**, 3 × 32 MiB byte-exact, rail delta exactly 96 |

For comparison, system-memory RDMA on the same rail measured 388.74 Gb/s
single-QP / 390.28 Gb/s sustained — GPUDirect GPU-memory transfers run at
full rail speed, with no host-memory staging.

## Why peermem cannot work here (negative control, root-caused)

The classic GPUDirect path registers GPU memory through the
`nvidia-peermem` kernel module. On these guests:

- `nvidia-peermem.ko` **is** built and version-matched (DKMS, 615.71.10,
  correct vermagic) — but `modprobe` fails with `Invalid argument`, with no
  dmesg trace.
- Root cause: the module's init calls `ib_register_peer_memory_client` /
  `ib_unregister_peer_memory_client`, and **those symbols do not exist in
  `/proc/kallsyms`** — the peer-memory-client API is MOFED-only and was never
  upstreamed. The guests run the inbox RDMA stack, so there is nothing to
  register with; init returns `-EINVAL`.
- Consistent negative control: perftest plain `--use_cuda` (peermem path)
  fails with `Couldn't allocate MR with error=14` — GPU memory cannot be
  pinned without a peer-memory client.

Vendor guidance (NVIDIA, via internal consultation on 2026-11-05): the
dma-buf route is the supported GPUDirect path with inbox mlx5 drivers
(`ibv_reg_dmabuf_mr` in rdma-core ≥ v34 + dmabuf export from the open kernel
modules); MOFED is not required, and its availability for the el10nv 64 KiB
page kernel is unconfirmed. Installing MOFED in the guests would also replace
the validated inbox stack — it was deliberately not pursued.

## Guest setup (both VMs)

perftest upstream (linux-rdma master, `8f87844`) has full CUDA dma-buf
support (`--use_cuda_dmabuf`, `--use_cuda_pcie_mapping`) plus an on-GPU
validation-kernel plugin. The RHEL 10 perftest RPM has none of this, so it
was built from source on both guests at `/tmp/perftest`:

```bash
sudo dnf -y install git autoconf automake libtool \
  libibverbs-devel librdmacm-devel pciutils-devel
# VM3 additionally: sudo dnf -y install cuda-toolkit-13-5   (VM12 already had 13.4)
git clone --depth 50 https://github.com/linux-rdma/perftest.git /tmp/perftest
cd /tmp/perftest
export CUDA_H_PATH=/usr/local/cuda-13.5/include/cuda.h      # 13.4 on VM12
export LDFLAGS="-L/usr/local/cuda-13.5/lib64/stubs"
export CPPFLAGS="-I/usr/local/cuda-13.5/include"
./autogen.sh && ./configure --enable-cudart && make -j2
sudo cp libperftest_kernels.so /usr/local/lib/ && sudo ldconfig
```

`python3-pyverbs` (rdma-core) is installed on both guests for the custom
test. Guest quirks that matter: perftest `--help`/`--version` exit 1, bare
`ib_write_bw` runs as a server and hangs, and `--data_validation` needs
~64 MiB pinned (guest memlock default is 8 MiB → run under
`sudo bash -c 'ulimit -l unlimited && …'`).

## Data-validation results (perftest `--data_validation`)

perftest's real-time receiver verification posts **FETCH_AND_ADD atomics**
against the receive buffer as chunk markers:

- **Host memory: PASS** — `VALIDATION: PASSED - 4992 chunks, 327155712
  bytes` at 389.54 Gb/s (root + unlimited memlock). This proves
  atomics-over-RoCE work on this fabric with host memory.
- **GPU memory (dma-buf): fails reproducibly** — client completions die with
  `transport retry counter exceeded (12) … syndrom 0x81` (responder-side
  protection error). FETCH_AND_ADD on dmabuf-registered GPU memory is not
  supported by the inbox mlx5 stack; this is a **documented limitation of
  the validation feature, not a setup defect** (the plain WRITE path to the
  same memory is clean and fast).

Because the built-in validator therefore cannot prove GPU-buffer *content*,
a custom test was written to do exactly that.

## Custom content-integrity test: `gpudirect-verify.py`

`gpudirect-verify.py` (this directory; also on the bastion and both guests)
performs the full GPUDirect flow by hand, without perftest:

- **CUDA side (ctypes, `libcuda.so.1`)**: `cuInit` → `cuCtxCreate` →
  `cuMemAlloc` (32 MiB) → page-aligned `cuMemGetHandleForAddressRange`
  (POSIX FD, handle type 1) — the same flow perftest uses. Patterns are
  written via `cuMemcpyHtoD`; verification reads back via `cuMemcpyDtoH`.
- **RDMA side (pyverbs)**: RC QP on the rail device, `DmaBufMR` on the
  exported fd, RoCE GRH addressing (GID index 3), client RDMA-Writes its
  GPU buffer to the server's GPU buffer in 1 MiB chunks, QP info exchanged
  over a plain TCP socket on the rail subnet.
- **Protocol**: server pre-fills its GPU buffer and verifies its own
  readback path first (sanity). Then 3 rounds with distinct positional
  patterns (byte = f(offset, round magic)); after each round the server
  reads back the **whole 32 MiB** and compares every byte. A
  corrupted/zero/stale buffer fails; each round's magic differs so stale
  data from the previous round is caught too.

Usage (server on VM3, client on VM12, rail `gpu0p0`):

```bash
python3 gpudirect-verify.py server 192.0.2.21 19500 --size-mib 32 --rounds 3
python3 gpudirect-verify.py client 192.0.2.21 19500 --size-mib 32 --rounds 3
```

**Result (2026-11-05, clean run, both exit 0):**

```
server: initial GPU fill/readback sanity: PASS (bad_tiles=0)
server: round 0 magic=0x5a verify: PASS bad_tiles=0/512 first_mismatch=None
server: round 1 magic=0x7f verify: PASS bad_tiles=0/512 first_mismatch=None
server: round 2 magic=0xa4 verify: PASS bad_tiles=0/512 first_mismatch=None
server: ALL 3 ROUNDS PASSED — GPU memory content byte-exact after RDMA writes
client: VERDICT: PASS — 3 rounds, 96 MiB total, 241.97 Gb/s average
RAIL gpu0p0 rx_write_requests delta = 96   (exactly 3 × 32 chunks)
```

The ~242 Gb/s figure is single-QP, single-threaded Python posting — this is
a correctness test; use perftest for bandwidth (390 Gb/s sustained on the
same rail).

### Implementation notes (hard-won API details)

- **CUDA symbol versioning**: `libcuda.so.1` exports *legacy v1* stubs under
  the plain names (`cuMemAlloc`, `cuMemcpyHtoD`, …); the real APIs are
  `cuMemAlloc_v2` / `cuMemcpyHtoD_v2` / `cuMemcpyDtoH_v2` / `cuCtxCreate_v2`
  (cuda.h `#define`s confirm the mapping). Calling the legacy symbols
  produced `CUDA_ERROR_INVALID_VALUE` from `cuMemGetHandleForAddressRange`
  and a segfault in `cuMemcpyHtoD`.
- **pyverbs `DmaBufMR` IOVA**: rdma-core registers with
  `ibv_reg_dmabuf_mr(pd, offset, length, iova=offset, fd, access)` — the
  MR's valid address range is **[offset, offset+length)**, MR-relative, not
  the GPU virtual address. Both the local SGE address and the remote write
  address must be expressed in that space (using the GPU VA gives
  `IBV_WC_LOC_PROT_ERR` / status 4).
- **pyverbs constructor quirks** (RHEL 10 rdma-core): `QPAttr` and `AHAttr`
  accept almost no init kwargs despite their docstrings — build them empty
  and assign attributes (`ah.dgid = '<gid string>'` — the setter takes a
  string, not a `GID` object). `SendWR` requires `sg=[SGE(...)]` (a list).
  `CQ.poll()` returns `(npolled, wcs)`; WC status is `wc.status`.
- **pyverbs teardown**: destroying objects during interpreter exit or
  cyclic-GC can segfault (after all results are already printed). The test
  calls `os._exit(0)` on success and disables cyclic GC.

## Limits and next steps

- GPU-mode `--data_validation` (atomics on dmabuf GPU memory) is not
  supported by the inbox stack — see above; content integrity is instead
  proven by `gpudirect-verify.py`.
- `nvidia-peermem`/MOFED path: not pursued by decision (dma-buf is the
  vendor-preferred inbox path; MOFED would replace the validated stack and
  its el10nv-64k support is unconfirmed).
- Only rail `gpu0p0` was content-verified; the other three rails use the
  identical mechanism (bandwidth was validated per-rail in the four-rail
  report). GPU-buffer latency (`ib_write_lat --use_cuda_dmabuf`) and the
  NUMA-1 rails with GPU buffers remain optional follow-ups.
- Guest tooling (source-built perftest, `libperftest_kernels.so`,
  `python3-pyverbs`, CUDA toolkit on VM3) lives in the guests' runtime
  environment; `/tmp/perftest` does not survive a guest reboot.

## Audit evidence

Bastion, `maintenance/20261105T115239Z-four-cx8-rails/`:
`gpudirect-negative-control.log` (peermem negative control, MR error=14),
`gpudirect-bw-gpu0p0-single-qp-vm12-to-vm3.log`,
`gpudirect-bw-gpu0p0-single-qp-vm3-to-vm12.log`,
`gpudirect-bw-gpu0p0-sustained-16qp-30s.log`,
`gpudirect-bw-two-rail-concurrent.log` (bandwidth suite, driver
`gpudirect-bw-rerun.py`), and `gpudirect-verify.py` +
`gpudirect-verify-run.log` (custom integrity test, final clean run).
`gpudirect-test.py` is the earlier one-shot suite driver (its
mid-run `nvidia-smi` evidence — 682 MiB on both VMs — comes from that
run's console output). All host-side state (rail maps, counters) is in the
same directory per the four-rail report.
