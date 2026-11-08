# 8 — RDMA & GPUDirect validation

The final acceptance tests: VM-to-VM RoCE RDMA with system memory, then
GPUDirect RDMA (GPU-memory ↔ GPU-memory buffers) with byte-exact content
verification. Full per-test evidence and raw logs are referenced from the
validation reports in `configs/07-rdma/`; this chapter is the stable procedure
plus the validated numbers.

## 8.1 Test-path discipline

Bandwidth alone proves nothing about the path. Every test:

- binds an explicit RDMA **device** (`-d mlx5_X`) and **GID index** (3),
- uses per-test TCP control ports on the rail subnet,
- is proven via the receiver's RDMA hardware counters
  (`rdma statistic show link <dev>/1` → `rx_write_requests` advancing by
  **exactly** the iteration count) — verbs traffic bypasses netdev stats.

perftest quirks to expect: `--help`/`--version` exit 1; a bare
`ib_write_bw` runs as a server and hangs; the 16-QP test needs `-q 16` on
the **server listener too** (QP count is not exchanged, unlike message
size/iters).

## 8.2 Manual test run (SSH runbook)

Everything below assumes you can SSH into both guests:

```bash
ssh -i ~/.ssh/hcp_rsa -p 30025 cloud-user@172.16.2.105    # gpu-vm-tray-3
ssh -i ~/.ssh/hcp_rsa -p 30024 cloud-user@172.16.2.114    # gpu-vm-tray-12
```

Run each step on both guests unless stated. **The server command always
starts first**; the client connects to the server's rail IP.

### Step 1 — check rail state and the current mapping

Guest HCA/netdev names shuffle between boots — rediscover, don't assume:

```bash
rdma link show
# expect four ports: link mlx5_X/1 state ACTIVE physical_state LINK_UP netdev enpYYYs0

# confirm each HCA's RoCEv2 GID (index 3 must be the v2 GID derived from
# that VF's IP — it only appears after the IP is set in Step 2):
for d in /sys/class/infiniband/mlx5_*; do
  echo "$(basename $d): $(cat $d/ports/1/gids/3) type=$(cat $d/ports/1/gid_attrs/types/3)"
done
```

Last-validated rail↔HCA↔IP mapping (2026-11-05 boot — verify, don't trust
blindly; netdev names come from `rdma link show`):

| Rail | Host VF | VM3 HCA / IP | VM12 HCA / IP | /30 |
|---|---|---|---|---|
| gpu0p0 | `0000:03:00.2` | `mlx5_1` / .21 | `mlx5_1` / .22 | 192.0.2.20/30 |
| gpu1p0 | `0002:03:00.2` | `mlx5_0` / .25 | `mlx5_0` / .26 | 192.0.2.24/30 |
| gpu2p0 | `0010:03:00.2` | `mlx5_2` / .29 | `mlx5_2` / .30 | 192.0.2.28/30 |
| gpu3p0 | `0012:03:00.2` | `mlx5_3` / .33 | `mlx5_3` / .34 | 192.0.2.32/30 |

If a reboot shuffled the guest PCI layout and the table no longer checks
out (Step 2 pings fail), re-derive the host-VF↔guest-slot binding from
the live domain XML (needs the managed kubeconfig):

```bash
oc exec -n gpu-vm-test $(oc get pod -l kubevirt.io/domain=gpu-vm-tray-3 -o name) \
  -c compute -- virsh dumpxml gpu-vm-test_gpu-vm-tray-3 | grep -A6 '<hostdev'
```

### Step 2 — bring up the test networking (runtime-only)

Skip if already configured (check with `ip addr`). Per rail, per guest —
VM3 takes .21/.25/.29/.33, VM12 takes .22/.26/.30/.34:

```bash
DEV=enp253s0                    # this rail's netdev (from rdma link show)
IP=192.0.2.21                   # this VM's IP on this rail
sudo nmcli device set $DEV managed no        # stop NetworkManager flushing it
sudo ip link set $DEV mtu 9000
sudo ip addr add $IP/30 dev $DEV
sudo sysctl -w net.ipv4.conf.$DEV.arp_ignore=1      # prevent ARP flux across rails
sudo sysctl -w net.ipv4.conf.$DEV.arp_announce=2
```

**Wait a few seconds after `ip addr add` before testing**: newly added
IPv4 addresses are *tentative* (duplicate-address detection) for a short
window, during which the kernel does not answer ARP for them — immediate
pings fail and park the neighbor entry in FAILED/backoff state, making a
working rail look broken.

Verify per rail (from VM3; repeat mirrored from VM12):

```bash
ping -c3 -I $DEV -M do -s 8972 192.0.2.22    # DF-set jumbo ping must pass
ip neigh show dev $DEV                        # peer MAC must be here …
ip neigh show | grep -v $DEV                  # … and NOT on the other rails
```

### Step 3 — system-memory RDMA (perftest)

Counter-based path proof first — on the **server** (VM3), snapshot the
receiving rail's counter:

```bash
rdma statistic show link mlx5_1/1 | grep rx_write_requests
```

Then the matched pair (rail gpu0p0 = `mlx5_1` on both guests):

```bash
# VM3 (server, starts first):
taskset -c 0 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18724 --report_gbits -s 65536 -n 5000

# VM12 (client):
taskset -c 0 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18724 --report_gbits -s 65536 -n 5000 192.0.2.21
```

Expect ~**386–390 Gb/s** and both processes exiting 0. Re-read the
counter on VM3: `rx_write_requests` must have advanced by **exactly
5000** — that is the proof the traffic used this rail.

Counter accounting note: in `-n` (fixed-iteration) mode the counter delta
equals the iteration count exactly. In `-D` (duration) mode with multiple
QPs, perftest's printed iteration count is **not** the hardware write
count — verify instead that `delta × message-size × 8 ÷ duration` equals
the reported bandwidth (e.g. 14.2M writes × 64 KiB × 8 ÷ 30 s ≈ 249 Gb/s
per rail).

Variants (same discipline — unique `-p` per concurrent pair, `-q 16` on
the **server too**, `-b` on both sides for bidirectional):

```bash
# sustained 16 QPs / 30 s:     add   -q 16 -D 30        (both sides)
# write latency:               ib_write_lat -d mlx5_1 -i 1 -x 3 -p 18726 -s 2 -n 1000
# reverse direction:           run the server on VM12, client targets 192.0.2.22
# two rails concurrently:      two listeners on VM3 (-p 18728 / -p 18729, taskset -c 0 / -c 1),
#                              two clients on VM12 against .21 and .25
```

Four rails concurrently — this is the test behind the headline
**1,560.84 Gb/s one-way / ~2,656 Gb/s bidirectional** numbers (four
independent streams summed, **not** a bond; full line rate per rail with
4 vCPUs — one rail per vCPU). Verified runnable by hand 2026-11-05:
repeated runs reproduced 992.83 / 994.39 Gb/s one-way at 2 vCPUs,
1,560.84 Gb/s at 4 vCPUs, and 1,943–2,657 Gb/s bidirectional across
configurations. Start the four listeners on VM3,
then the four clients on VM12 simultaneously (within a few seconds):

```bash
# VM3 — four listeners, one rail per vCPU (4 vCPUs per VM):
taskset -c 0 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18731 --report_gbits -s 65536 -q 16 -D 30 > /tmp/r-gpu0p0.log 2>&1 &
taskset -c 1 ib_write_bw -d mlx5_0 -i 1 -x 3 -p 18732 --report_gbits -s 65536 -q 16 -D 30 > /tmp/r-gpu1p0.log 2>&1 &
taskset -c 2 ib_write_bw -d mlx5_2 -i 1 -x 3 -p 18733 --report_gbits -s 65536 -q 16 -D 30 > /tmp/r-gpu2p0.log 2>&1 &
taskset -c 3 ib_write_bw -d mlx5_3 -i 1 -x 3 -p 18734 --report_gbits -s 65536 -q 16 -D 30 > /tmp/r-gpu3p0.log 2>&1 &

# VM12 — four clients, all concurrent:
taskset -c 0 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18731 --report_gbits -s 65536 -q 16 -D 30 192.0.2.21 > /tmp/c-gpu0p0.log 2>&1 &
taskset -c 1 ib_write_bw -d mlx5_0 -i 1 -x 3 -p 18732 --report_gbits -s 65536 -q 16 -D 30 192.0.2.25 > /tmp/c-gpu1p0.log 2>&1 &
taskset -c 2 ib_write_bw -d mlx5_2 -i 1 -x 3 -p 18733 --report_gbits -s 65536 -q 16 -D 30 192.0.2.29 > /tmp/c-gpu2p0.log 2>&1 &
taskset -c 3 ib_write_bw -d mlx5_3 -i 1 -x 3 -p 18734 --report_gbits -s 65536 -q 16 -D 30 192.0.2.33 > /tmp/c-gpu3p0.log 2>&1 &
wait
grep -H -E "^\s*65536\s" /tmp/c-gpu*.log    # per-rail results
```

For the bidirectional figure add `-b` to all eight processes (listeners
**and** clients). Expect ~**390 Gb/s per rail** one-way (full line rate;
at 2 vCPUs per VM it was ~248) and ~775 (NUMA-0 rails) / ~525–578
(NUMA-1 rails) full-duplex bidirectional. Path proof: each
rail's `rx_write_requests` delta × 64 KiB × 8 ÷ 30 s should equal that
rail's reported Gb/s (~22.3M writes ≈ 390 Gb/s).

**Reading the results after the run** — both sides print the same result
table; on VM3 the listeners' output is in `/tmp/r-gpu*.log`:

```bash
grep -h -E "^\s*65536\s" /tmp/r-gpu*.log | \
  awk '{s+=$4; print} END {printf "TOTAL: %.2f Gb/s\n", s}'
```

Columns are `#bytes  #iterations  BW peak  BW average  MsgRate`: read
**BW average** (Gb/s with `--report_gbits`); `BW peak` is always 0.00 in
`-D` duration mode; `#iterations` is perftest's internal count, not the
hardware write count. Use `grep -h` (lowercase — no filename prefix)
when summing with awk: with `-H` the filename becomes `$1` and shifts
the columns, so `$4` would silently sum the always-zero peak column.
To see which rail each row belongs to, use `grep -H` without the awk.
Server-side and client-side totals differ by up to ~1% (each times its
own completions) — either is valid. The header block doubles as a
sanity check: per-log `Device`, `Number of qps: 16`,
`GID index: 3`, and the local/remote GIDs (ending `c000:02xx`) confirm
which rail and peer each stream used.

**Restarting the test.** After a completed run there is nothing to clean
up — both sides exit on their own and the control ports are free; just
re-run the command blocks (archive `/tmp/[rc]-gpu*.log` first if you
want to keep the previous results — `>` overwrites). If a run was
interrupted or a listener is stuck:

```bash
pgrep -ax ib_write_bw      # what's still running
pkill -x ib_write_bw       # kill by EXACT name — never `pkill -f`, which
                           # also matches (and kills) your own ssh shell
ss -tlnp | grep 1873       # control ports must be free before re-running
```

If a port refuses to bind (TIME_WAIT), bump all four `-p` values by +10
**consistently on both sides**. After any guest/worker reboot, re-verify
rail state and re-apply the runtime networking (Steps 1–2) before
testing.

The one-shot wrapper `configs/07-rdma/rdma-vm2vm-test.sh` automates this
basic suite (server/client modes, GID auto-discovery, PASS/FAIL exit
code) — the manual commands above are what it runs.

### Step 4 — GPUDirect RDMA (GPU-memory buffers)

Requires the source-built perftest (Section 8.4 tooling — `/tmp/perftest`
does **not** survive a guest reboot; rebuild if missing) and `nvidia-smi`
healthy. Same matched-pair pattern, with the dma-buf flags:

```bash
# VM3 (server):
taskset -c 0 /tmp/perftest/ib_write_bw -d mlx5_1 -i 1 -x 3 -p 19100 \
  --use_cuda=0 --use_cuda_dmabuf --report_gbits -s 65536 -n 5000

# VM12 (client):
taskset -c 0 /tmp/perftest/ib_write_bw -d mlx5_1 -i 1 -x 3 -p 19100 \
  --use_cuda=0 --use_cuda_dmabuf --report_gbits -s 65536 -n 5000 192.0.2.21
```

While it runs, confirm in a third shell on **each** guest that the
buffers are on-GPU:

```bash
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
# expect ~680 MiB held by ib_write_bw
```

Expect ~**386–390 Gb/s** (same as system memory) and the rail counter
delta exactly 5000. A plain `--use_cuda=0` (without `_dmabuf`) is the
expected-to-fail negative control — see Appendix A.7.

### Step 5 — byte-exact content verification

Requires `python3-pyverbs` on both guests and the test script copied to
both (from a checkout of this repo):

```bash
scp -P 30025 configs/07-rdma/gpudirect-verify.py cloud-user@172.16.2.105:/tmp/
scp -P 30024 configs/07-rdma/gpudirect-verify.py cloud-user@172.16.2.114:/tmp/

# VM3 (server):
python3 /tmp/gpudirect-verify.py server 192.0.2.21 19500 --size-mib 32 --rounds 3
# VM12 (client):
python3 /tmp/gpudirect-verify.py client 192.0.2.21 19500 --size-mib 32 --rounds 3
```

Expect per round `verify: PASS bad_tiles=0/512`, the final
`ALL 3 ROUNDS PASSED` line, both processes exit 0, and the gpu0p0
counter delta of exactly 96 (3 rounds × 32 chunks).

## 8.3 System-memory RDMA (validated 2026-11-05)

Per rail: 64 KiB messages, pinned processes, both directions. Results:

| Test | Result |
|---|---|
| Single-QP, 5,000 writes, per rail, both directions | **383.02–388.74 Gb/s** |
| Sustained per rail (16 QPs, 30 s) | **390.28 Gb/s** (97.6% of 400G) |
| 4 rails concurrent, one-way (VM12→VM3), 4 vCPUs | **1,560.84 Gb/s** — **full line rate on every rail** (390.08–390.28) |
| 4 rails concurrent, bidirectional, 4 vCPUs | **~2,656 Gb/s** aggregate (NUMA-0 rails ~775–778 full-duplex; NUMA-1 rails ~525–578) |
| 4 rails concurrent, one-way, at 2 vCPUs (before the 4-vCPU change) | 992–994 Gb/s (per rail ~248) |
| Write latency, 2 B × 1,000 (NUMA-0 rails) | **2.34–2.38 µs** avg |
| Write latency (NUMA-1 rails) | **3.15–3.23 µs** avg (cross-socket) |
| Path proof | rail counter delta matches the reported traffic exactly per rail |

With **one rail per vCPU** (4 vCPUs per VM), every rail sustains its
isolation speed under four-way concurrency — the earlier per-rail
~248 Gb/s figure was purely the 2-vCPU configuration's CPU ceiling, not a
fabric or VF limit. In **bidirectional** mode each stream doubles its CPU
work; the NUMA-0 rails still reach ~775–778 Gb/s full-duplex, while the
NUMA-1 rails (cross-socket DMA) settle at ~525–578. **No bond/LAG is
configured** — aggregates are simultaneous independent streams; generic
Ethernet bonding must not be assumed to aggregate RDMA.

A self-contained wrapper for the basic suite is
`configs/07-rdma/rdma-vm2vm-test.sh` (server + client modes, GID
auto-discovery, counter-based isolation proof, PASS/FAIL exit code).

Example matched commands (16-QP sustained, one rail):

```bash
# VM3 (server):   taskset -c 0 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18724 --report_gbits -s 65536 -q 16 -D 30
# VM12 (client):  taskset -c 0 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18724 --report_gbits -s 65536 -q 16 -D 30 192.0.2.21
```

(Use each guest's *current* HCA/GID mapping — names shuffle between boots.)

## 8.4 GPUDirect RDMA — dma-buf path (validated 2026-11-05)

**GPU-memory ↔ GPU-memory RDMA works between the VMs** using
`ibv_reg_dmabuf_mr` + CUDA `cuMemGetHandleForAddressRange` on the **inbox
mlx5 stack** — no MOFED, no `nvidia-peermem`. This is the supported path
with inbox drivers (vendor-confirmed); the peermem module cannot work here
because `ib_register_peer_memory_client` is a MOFED-only kernel API absent
from the guest kernels (negative control documented in Appendix A).

### Guest tooling

The RHEL 10 perftest RPM lacks CUDA support — build from source on both
guests:

```bash
sudo dnf -y install git autoconf automake libtool \
  libibverbs-devel librdmacm-devel pciutils-devel
# VM3 additionally: sudo dnf -y install cuda-toolkit-13-5  (VM12 has 13.4)
git clone --depth 50 https://github.com/linux-rdma/perftest.git /tmp/perftest
cd /tmp/perftest
export CUDA_H_PATH=/usr/local/cuda-13.5/include/cuda.h
export LDFLAGS="-L/usr/local/cuda-13.5/lib64/stubs"
export CPPFLAGS="-I/usr/local/cuda-13.5/include"
./autogen.sh && ./configure --enable-cudart && make -j2
sudo cp libperftest_kernels.so /usr/local/lib/ && sudo ldconfig
sudo dnf -y install python3-pyverbs
```

### Results (rail gpu0p0, VM12↔VM3)

| Test | Result |
|---|---|
| `ib_write_bw --use_cuda=0 --use_cuda_dmabuf`, 1 QP, both directions | **386.10 / 386.21 Gb/s** |
| Sustained (16 QPs, 30 s) | **390.25 Gb/s** — identical to system memory |
| Two GPU-local rails concurrent | **780.02 Gb/s** (389.93 + 390.09) |
| Four rails concurrent, all buffers on GPU 0 | **1,165.29 Gb/s** — single-GPU host-interface bound |
| Four rails concurrent, buffers split GPU 0 + GPU 1 | **1,560.63 Gb/s** — **full line rate on every rail** (390.10–390.26) |
| Mid-run `nvidia-smi` on both VMs | **682 MiB** GPU memory held by `ib_write_bw` (buffers on-GPU) |
| Path proof | rail counter delta exactly 5,000 per single-QP run |

GPU-memory transfers run at full rail speed — no measurable penalty vs
host-memory staging. The four-rail concurrent results expose an important
property: with **all streams' buffers on one GPU**, aggregate GPUDirect
caps at ~1.17 Tb/s (the single GPU's shared host interface becomes the
bottleneck, and per-rail throughput is uneven); splitting the buffers
across **both** VM GPUs restores full line rate on every rail. A real
multi-rail application (NCCL) distributes buffers across GPUs naturally —
keep that in mind when designing multi-rail GPUDirect workloads.

### Data-content integrity — `gpudirect-verify.py`

⚙ **CUSTOM COMPONENT — `configs/07-rdma/gpudirect-verify.py`.** *Problem:*
perftest's `--data_validation` cannot verify GPU buffers on this stack
(its chunk markers are FETCH_AND_ADD atomics, unsupported on dmabuf GPU
memory — documented limitation, Appendix A), so a byte-exact content
proof needed a purpose-built test. *Fix:* a pyverbs + ctypes test that
allocates GPU memory on both VMs (CUDA VMM-equivalent flow), registers it
as a dmabuf MR, RDMA-writes 3 rounds × 32 MiB of **positional patterns**
(distinct magic per round, so stale data fails), and the server verifies
**every byte** via `cuMemcpyDtoH` readback:

```bash
# VM3: python3 gpudirect-verify.py server 192.0.2.21 19500 --size-mib 32 --rounds 3
# VM12: python3 gpudirect-verify.py client 192.0.2.21 19500 --size-mib 32 --rounds 3
```

Validated result (clean run, both exit 0): all rounds
`bad_tiles=0/512 first_mismatch=None`, rail counter delta **exactly 96**
(3 × 32 chunks). ~242 Gb/s single-QP Python-driven — a correctness test;
use perftest for bandwidth.

The script's header comments document the API details that made it
work: CUDA **versioned symbols** (`cuMemAlloc_v2` etc. — the unversioned
exports are legacy v1 stubs), pyverbs `DmaBufMR` **iova = offset**
semantics (MR-relative addressing on both SGE and remote address), and
the pyverbs constructor quirks of RHEL 10's rdma-core.

### Host-memory validation control

perftest `--data_validation` on **host** memory **passes**
(`VALIDATION: PASSED - 4992 chunks, 327155712 bytes`, 389.54 Gb/s) —
run as root with `ulimit -l unlimited` (the ~64 MiB validation buffer
exceeds the default 8 MiB guest memlock). This proves atomics-over-RoCE
work on this fabric; the GPU-mode failure is specific to atomics on
dmabuf memory.

## 8.5 Validation reports (full evidence)

| Report | Contents |
|---|---|
| `../archive/07-rdma/four-rail-validation-2026-11-05.md` | rail mapping, jumbo-MTU procedure, system-memory results |
| `../archive/07-rdma/gpudirect-validation-2026-11-05.md` | GPUDirect results, peermem root-cause, integrity test |
| `../archive/07-rdma/two-rail-validation-2026-11-05.md` | two-rail fallback profile (MTU 1500/1024) |
| `../archive/07-rdma/recovery-2026-11-05.md` | one-VF fallback baseline |

Raw logs, before/after states, scripts and counter deltas are preserved
on the bastion under
`/home/nvidia/workspaces/skaufman/task_hcp/maintenance/` (per-report
directories, listed in each report's audit-evidence section).

## 8.6 Known limits of the validated state

- Guest test networking and host PF MTU 9000 are **runtime-only** —
  re-apply after any worker or guest reboot (host CX-8 boot provisioning
  is a separate task). Note the IPv4 DAD window: wait a few seconds after
  `ip addr add` before testing (Section 8.2, Step 2).
- Bidirectional concurrency: NUMA-0 rails reach ~775–778 Gb/s
  full-duplex, but the NUMA-1 rails settle at ~525–578 (cross-socket DMA
  plus doubled per-stream CPU work). One-way concurrency is at full line
  rate on all rails.
- Multi-rail GPUDirect: aggregate is bound by the GPUs' host interfaces
  unless buffers are spread across GPUs (see the Section 8.4 results);
  a single multi-rail application (NCCL-style) remains untested.
- No bond/LAG/ECMP; aggregates are independent streams.
- GPUDirect content-verified on rail `gpu0p0`; the other rails use the
  identical mechanism (bandwidth validated per rail). GPU-buffer latency
  and NUMA-1 rails with GPU buffers remain optional follow-ups.
- One plane per GPU (400G GPU-local per GPU); the port-1 PFs for
  dual-plane 800G per GPU are idle — a separate profile change.
- Production IPAM/VLAN and PFC/ECN lossless tuning are pending fabric
  input; no loss was observed in any validation run, but congestion
  behavior is untested — lossless Ethernet (PFC/ECN or an adaptive-routing
  fabric) is a production prerequisite for RDMA under load.
- PCIe ACS was **not** disabled on the workers; GPUDirect still measured
  at full line rate on this VM-passthrough path. Boot-time ACS disable is
  worth considering for bare-metal (non-VM) deployments of this hardware,
  where GPU↔NIC peer-to-peer DMA can otherwise be redirected through the
  root complex.
- The inbox mlx5 stack sustains line-rate GPUDirect via DMA-BUF in this
  topology; DOCA/MOFED remains untested on the el10nv 64 KiB-page guest
  kernel and is only needed for peermem or DCB tooling.
- Guest memlock: `cloud-user` now has `memlock unlimited` in
  `/etc/security/limits.conf` on both guests (applied 2026-11-05); other
  non-root RDMA users need the same entry.
- `gdrcopy` (fast CPU-driven copy to/from GPU memory, cuts small-message
  GPU RDMA latency) is not installed in these guests — optional follow-up
  if small-message GPU RDMA latency matters.
