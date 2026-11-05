# Four CX-8 rails per VM + jumbo MTU — validated 2026-11-05

## Current tested profile

- `gpu-vm-tray-3` and `gpu-vm-tray-12`: **two GPU requests and four CX-8 VF
  hostDevices each**, guest SSH working, all four RDMA ports ACTIVE.
- `gpu-vm-tray-11` remains Halted. No BF-3 VF is attached to the active pair.
- **Jumbo MTU is enabled end-to-end**: Ethernet 9000 on the four rail PFs on
  each worker and on all guest VF netdevs; **active RDMA MTU 4096** on every
  port (previously 1500/1024). Fabric support was verified empirically (see
  below), not assumed.
- **This is not bonding.** Aggregates below are sums of simultaneous,
  device/GID-pinned perftest streams on four separate VFs.

## The four rails

Both workers have a symmetric layout: eight CX-8 PFs (four per NUMA node),
each 400G. The four selected rails use the port-0 PF of each pair:

| Rail | PF | NUMA | VF | IOMMU group |
|---|---|---|---|---|
| `gpu0p0` | `0000:03:00.0` | 0 | `0000:03:00.2` | 93 |
| `gpu1p0` | `0002:03:00.0` | 0 | `0002:03:00.2` | 94 |
| `gpu2p0` | `0010:03:00.0` | 1 | `0010:03:00.2` | 95 |
| `gpu3p0` | `0012:03:00.0` | 1 | `0012:03:00.2` | 96 |

All four VFs already existed, bound to `vfio-pci` in singleton IOMMU groups
on both workers — no VF creation or rebinding was needed. The NUMA-1 VFs
were the "unused VFs on other PFs" left in place by the
[one-VF recovery](recovery-2026-11-05.md).

Guest names shuffle between boots and differ per VM; they were mapped
authoritatively via libvirt XML (host VF source), QMP `query-pci`
(guest PCI by device alias) and guest sysfs (netdev/HCA). The discovered
mapping is recorded in `rail-map-before-network.json` on the bastion.

**NUMA caveat:** each VM's GPUs (`0008`/`0009`) and pinned vCPUs are on
NUMA node 0. Rails `gpu2p0`/`gpu3p0` are NUMA-1 devices: cross-socket DMA.
This is visible in latency (3.23 us vs 2.38 us average) and slightly lower
single-QP bandwidth. Two links per GPU on the GPU-local socket would require
using the port-1 PFs (`.1` functions, currently unused, `sriov_numvfs 0`).

## Change sequence and safety gates

Full VM/VMI/node/plugin state was backed up on the bastion before changes
(`maintenance/20261105T115239Z-four-cx8-rails/`). VM12 was changed first and
guest SSH + 4 NICs verified; VM3 followed after that passed. The PCI
bus-range guard was retained; post-hook expander ranges were verified
disjoint with four hostdevs (VM3: `254-255`, `251-253`, `249-250`;
VM12: `253-255`, `251-252`, `249-250`). Rollback to the two-rail profile
was prepared but not needed. No worker reboot occurred; both CUDA driver
probes still pass after the change.

## Jumbo MTU: empirical fabric verification

The four rail PFs carry no host networking (no IPv4; link-local IPv6 at
most), so host-side MTU changes are safe. Sequence: host PF MTU 9000 via
virt-handler `nsenter`, then guest VF MTU 9000 with the test addresses.

Verification, in order:

1. All eight guest ports report `active_mtu: 4096` via `ibv_devinfo`.
2. **DF-set max-size pings** (`ping -M do -s 8972`, 9000-byte IP packet)
   passed on **all four rails in both directions** — the fabric switches
   demonstrably carry 9000-byte frames end-to-end.
3. Had any failed, the prepared rollback would have restored 1500
   everywhere and re-verified at 1500. It was not needed.

Host PF MTU is a **runtime change only** — after a worker reboot the PFs
return to 1500 (host CX-8 boot provisioning remains a separate task). Guest
test settings (IPs, ARP sysctls, NM-unmanaged, MTU) are also runtime-only.

## Test results

64 KiB messages, 16 QPs, 30-second runs, processes pinned two rails per
guest vCPU (gpu0p0/gpu2p0 on CPU 0, gpu1p0/gpu3p0 on CPU 1).

Independent correctness checks (one QP, 5,000 writes, both directions):
every run incremented only the selected rail's receiver `rx_write_requests`
by exactly 5,000; all other rails' counters stayed unchanged.

Sustained per-rail (jumbo, 16 QPs):

- **Each of the four rails: 390.28 Gb/s** (97.6% of 400G line rate).
- Single-QP writes: 383.02–388.74 Gb/s per rail, both directions.
- At MTU 1024 the same test measured 363.77 Gb/s — jumbo is worth
  **about +7%** sustained and about +20% single-QP, matching the
  expectation set before the change.

Concurrent four-rail:

- One-way, VM12 -> VM3: **993.90 Gb/s combined** (per-rail ~248 Gb/s).
- Bidirectional: **1,976.09 Gb/s aggregate** (~1.98 Tb/s), per-rail
  ~492–497 Gb/s full-duplex.
- Per-rail throughput under four-way concurrency is **guest-CPU-bound**
  (2 vCPUs, two streams each), not fabric- or VF-bound: each rail sustains
  390 Gb/s in isolation. More vCPUs or a single multi-rail process (NCCL)
  would be needed to saturate four rails fully.

Write latency, 2 bytes, 1,000 iterations:

- NUMA-0 rails `gpu0p0`/`gpu1p0`: **2.34–2.38 us** average.
- NUMA-1 rails `gpu2p0`/`gpu3p0`: **3.15–3.23 us** average
  (cross-socket penalty, as expected).

## Reproducible profile

To advertise four devices, apply `13-cx8-four-rails-plugin.patch.json`
to both CX-8 DaemonSets (`cx8-shim`, `cx8-shim-tray12`), then change one
stopped VM at a time with `12-cx8-four-rails-vm.patch.yaml`, keeping the
guard annotation. Wait for guest SSH and four active NICs before changing
the second VM. Both files are **patches, not apply manifests**; never
bulk-apply this directory.

For jumbo MTU: set the four rail PFs to 9000 on each worker, then 9000 on
the guest VF netdevs before adding addresses. Verify with `ibv_devinfo`
(active MTU 4096) and DF-set 8972-byte pings on every rail. The full
automated sequence with rollback is preserved on the bastion as
`jumbo-change.py`; the test suite is `four-rail-test.py`.

## Limits and next steps

- Aggregate results are concurrent independent streams, not a bond; no
  LAG/ECMP is configured or implied.
- Concurrent per-rail throughput is limited by the guests' 2 vCPUs.
  CPU-side scaling (more vCPUs, NCCL-style multi-rail process) is untested.
- NUMA-1 rails work but are not GPU-local for these VMs. A "two links per
  GPU" topology on the GPU-local socket would use the unused port-1 PFs.
- Host PF MTU 9000 and all guest test networking are runtime-only, not
  reboot-persistent; production IPAM/VLAN design is still pending.
- GPUDirect RDMA is validated separately (dma-buf path, see
  [GPUDirect validation](gpudirect-validation-2026-11-05.md)); the results
  in this report are system-memory RDMA.
- Rollback: stop the VM, restore the two-rail `hostDevices` from
  `10-cx8-two-rails-vm.patch.yaml`, restore the shim's two-device list
  from `11-cx8-two-rails-plugin.patch.json`, and optionally restore
  MTU 1500 on PFs/guests. Do not delete disks or reset workers.

Audit evidence on the bastion under
`maintenance/20261105T115239Z-four-cx8-rails/`: state backups, rail maps,
`guest-four-rail-config.json`, `rdma-four-rail-results.json`,
`jumbo-change.py`, `four-rail-test.py`, and all per-test logs.
