# Two CX-8 rails per VM — validated 2026-11-05

## Current tested profile

- `gpu-vm-tray-3` and `gpu-vm-tray-12`: **two GPU requests and two CX-8 VF
  hostDevices each**, guest SSH working and both RDMA ports ACTIVE.
- `gpu-vm-tray-11` remains Halted. No BF-3 VF is attached to the active pair.
- The selected plane-0 PFs on each physical worker are `0000:03:00.0` and
  `0002:03:00.0`, each link-up at 400G with Ethernet MTU 1500. Their VFs are
  `0000:03:00.2` and `0002:03:00.2`, both `15b3:101e`, bound to `vfio-pci`
  in separate one-device IOMMU groups. Both PFs are on NUMA node 0.
- Guest GPU allocations stayed on host functions `0008:06:00.0` and
  `0009:06:00.0`. The guest PCI layout retains AIE's NUMA and GPU memory
  mappings with the tested bus-range guard from the
  [one-VF recovery](recovery-2026-11-05.md).
- **This is not bonding.** Aggregate numbers below are sums of simultaneous,
  device/GID-pinned perftest streams on two separate VFs.

## Change sequence and safety gates

Before changing anything, the VM/VMI, node, HCO and plugin configurations
were saved on the bastion. The second VF already existed; no new PF link,
firmware setting, MTU or VF count needed changing.

VM12 was changed and guest SSH verified first; VM3 was changed separately
after that passed. Each shim now advertises only the two selected VFs. The
procedure was prepared to restore the one-VF profile if boot/SSH failed.
No rollback was necessary, and no worker reboot occurred.

The hook was retained on both VMs. Captured post-hook expander ranges were
disjoint: VM3 `253-255`, `251-252`, `249-250`; VM12 `254-255`, `252-253`,
`249-251`. The lab workaround is still needed; no upstream fix is asserted.

## Authoritative rail mapping

Guest names cannot be assumed identical between VMs. Libvirt XML provided
each host VF source address, QMP `query-pci` provided the actual guest PCI
address by device alias, and guest sysfs/RDMA links supplied the netdev/HCA.

Rail `gpu0p0` — physical PF `0000:03:00.0`, VF `0000:03:00.2`:

- VM3: guest PCI `0000:fe:00.0`, netdev `enp254s0`, HCA `mlx5_0`, test IP
  `192.0.2.21/30`.
- VM12: guest PCI `0000:fa:00.0`, netdev `enp250s0`, HCA `mlx5_0`, test IP
  `192.0.2.22/30`.

Rail `gpu1p0` — physical PF `0002:03:00.0`, VF `0002:03:00.2`:

- VM3: guest PCI `0000:ff:00.0`, netdev `enp255s0`, HCA `mlx5_1`, test IP
  `192.0.2.25/30`.
- VM12: guest PCI `0000:fb:00.0`, netdev `enp251s0`, HCA `mlx5_1`, test IP
  `192.0.2.26/30`.

The /30s are temporary TEST-NET-1 lab addressing, with no gateway. No public
`192.0.3.0/24` or similar network was configured. The routes to each peer were
verified to use the corresponding CX-8 interface, never the virtio default.
NetworkManager was made unmanaged only on the test interfaces.

Both rails use Ethernet MTU 1500 and active RDMA MTU 1024. Each interface's
IPv4-mapped RoCEv2 GID was discovered from sysfs; its index was 3 on this boot.

## Additional issue: ARP flux across the two interfaces

The initial multi-NIC ping lost packets despite correct addresses and routes.
VM12's neighbor entry for first-rail peer `192.0.2.21` contained the MAC of
VM3's **second** VF. Both interfaces had Linux's default `arp_ignore=0` and
`arp_announce=0`; the host-wide weak ARP behavior allowed a reply across rails.
Strict reverse-path filtering was also enabled in both guests.

Only the CX-8 guest interfaces were changed, at runtime:

```bash
# Run for each discovered CX-8 netdev in each guest, not enp1s0:
sudo sysctl -w net.ipv4.conf.<cx-netdev>.arp_ignore=1
sudo sysctl -w net.ipv4.conf.<cx-netdev>.arp_announce=2
```

The old values were saved in each guest at
`/var/tmp/cx8-two-rails-arp-before.json`. Only the test peer's neighbor entry
on each test interface was removed, then ARP relearned dynamically. Five
pings passed in every direction on each rail; all four neighbor MACs matched
the intended peer VF. Management settings, global sysctls, reverse-path
filtering and firewall settings were not changed.

## Fresh test results

All client and server processes exited zero. Each test bound a specific HCA,
port 1, discovered GID and distinct control port. Bandwidth used 64 KiB
messages; sustained tests used 16 QPs for 30 seconds. Processes were pinned
to guest CPU 0 for rail `gpu0p0` and CPU 1 for `gpu1p0`; each VM has two
dedicated vCPUs. These are system-memory tests, not GPU-buffer tests.

Independent correctness/bandwidth checks, one QP and 5,000 writes:

- Rail `gpu0p0`: VM12 -> VM3 **324.15 Gb/s**; VM3 -> VM12 **324.80 Gb/s**.
- Rail `gpu1p0`: VM12 -> VM3 **324.98 Gb/s**; VM3 -> VM12 **324.87 Gb/s**.
- In every run, only the chosen rail's receiver `rx_write_requests`
  increased by exactly 5,000. The other rail's write counter stayed unchanged.

Sustained bandwidth and concurrency:

- Rail `gpu0p0` alone, VM12 -> VM3: **363.77 Gb/s**.
- Rail `gpu1p0` alone, VM12 -> VM3: **363.77 Gb/s**.
- Both concurrently, VM12 -> VM3: **363.38 + 363.76 = 727.14 Gb/s combined**.
- Both concurrently, bidirectional: **723.02 + 725.46 = 1,448.48 Gb/s
  aggregate across both directions and both rails** (about 1.45 Tb/s).
- Client process lifetimes overlapped for approximately 31.47 and 31.45
  seconds respectively; both tests requested 30-second measurement runs.
  These are concurrent per-stream averages, not a measurement of a bonded
  interface or one QP spanning two ports.

Write latency, 2 bytes and 1,000 iterations:

- `gpu0p0`: **2.26 us min / 2.34 us avg**.
- `gpu1p0`: **2.30 us min / 2.36 us avg**.

Post-test checks: both guest SSH endpoints and agents remain healthy; both
workers and their boot IDs are unchanged. VM12's CUDA driver probe still
reports two GPUs and passes context creation and a 256 MiB allocation/free.
VM3's guest CUDA driver installation remains unvalidated, as before.

## Matched example commands

For the concurrent unidirectional run, run both listeners in VM3 and both
clients in VM12. Use each guest's actual HCA and GID mapping, not blindly
copied names/indexes after a future reboot.

```bash
# VM3: separate terminals/processes, one listener per rail.
taskset -c 0 ib_write_bw -d mlx5_0 -i 1 -x 3 -p 18724 --report_gbits -s 65536 -q 16 -D 30
taskset -c 1 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18725 --report_gbits -s 65536 -q 16 -D 30

# VM12: start both clients concurrently, not one after another.
taskset -c 0 ib_write_bw -d mlx5_0 -i 1 -x 3 -p 18724 --report_gbits -s 65536 -q 16 -D 30 192.0.2.21
taskset -c 1 ib_write_bw -d mlx5_1 -i 1 -x 3 -p 18725 --report_gbits -s 65536 -q 16 -D 30 192.0.2.25
```

For bidirectional tests, add `-b` on **both** listeners and both clients,
using fresh matching control ports.

## Reproducible profile and audit evidence

The lab hook, pinned image and distinct-resource shim endpoints are from
the one-VF baseline. To advertise two devices, apply the JSON patch
`11-cx8-two-rails-plugin.patch.json` to each of the two CX-8 DaemonSets.
For example:

```bash
oc patch ds cx8-shim -n sriov-standalone --type=json \
  --patch-file=07-rdma/11-cx8-two-rails-plugin.patch.json
oc patch ds cx8-shim-tray12 -n sriov-standalone --type=json \
  --patch-file=07-rdma/11-cx8-two-rails-plugin.patch.json
```

Then change one stopped VM at a time using
`10-cx8-two-rails-vm.patch.yaml`, preserving the guard annotation. Wait for
guest SSH and two active NICs before changing the second VM. Both patch
files are **patches, not apply manifests**. Never bulk-apply this directory,
which also contains superseded and incident-causing configurations.

On the bastion, full configurations, QMP/XML mapping, raw logs, exact
commands, return codes, timings and per-HCA counter deltas are saved under:
`/home/nvidia/workspaces/skaufman/task_hcp/maintenance/20261105T103244Z-two-cx8-rails/`.
The key files are `guest-two-rail-config.json`, `rdma-two-rail-results.json`,
`two-rail-change.py`, `two-rail-test.py`, and the per-test server/client logs.

## Limits and next steps

- No Linux bond, RoCE LAG, ECMP, jumbo MTU, or switch change was configured.
  Generic Ethernet bonding must not be assumed to provide RDMA aggregation.
- This is **two 400G VF links per VM**, not two links per GPU. Two links per
  GPU with two GPUs would require four VF links and separate validation.
- No four-rail bandwidth or GPUDirect RDMA result is implied by these tests.
- Temporary IPs, unmanaged state and per-interface ARP settings are not a
  production IPAM design or qualified reboot-persistent configuration. Host
  CX-8 VF boot provisioning also remains a separate reliability task.
- To roll back, stop the selected VM, restore its one-VF device list while
  keeping the PCI guard, and restore the selected shim to VF `0000:03:00.2`
  only. Restore guest test-interface settings from the saved before-state
  if needed. Do not delete disks, reset workers or remove the guard while
  retaining a topology that can overlap PCI ranges.
