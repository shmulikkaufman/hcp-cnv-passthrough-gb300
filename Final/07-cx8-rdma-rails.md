# 7 — CX-8 RDMA rails: VF supply, device shims, 4-rail attach, jumbo MTU

Attach four ConnectX-8 VFs per VM and bring up the RDMA data plane. All
manifests and patches live in `configs/07-rdma/`. **This directory contains
patches and superseded/historical files — apply only what each step names,
never bulk-apply it.**

The stock SR-IOV operator path cannot be used end-to-end on this platform.
Three gaps are bridged by small custom components, each marked ⚙ below and
indexed in [Appendix B](appendix-b-custom-components.md):

| Gap (stock component) | Bridge |
|---|---|
| SR-IOV policy layer reboot-loops the nodes on this hardware | VF-supply DaemonSet (runtime sysfs) |
| SR-IOV device plugin emits `PCIDEVICE_*`; KubeVirt launcher reads `PCI_RESOURCE_*` | cx8-shim device plugin |
| KubeVirt internal plugin misses kernel-6.x per-device vfio cdevs (CNV-91802, fixed in CNV 5.0) | cx8-shim `DeviceSpec`s |

(The SR-IOV operator itself stays installed for inventory only — **no
policies exist**, deliberately; see the incident record in Appendix A.)

## 7.1 Rail selection

Both workers have a symmetric layout: eight CX-8 PFs (four per NUMA node),
each 400G. The final profile uses the **port-0 PF of each pair** on both
workers:

| Rail | PF | NUMA | VF | guest test /30 (VM3 / VM12) |
|---|---|---|---|---|
| `gpu0p0` | `0000:03:00.0` | 0 | `0000:03:00.2` | 192.0.2.21 / .22 |
| `gpu1p0` | `0002:03:00.0` | 0 | `0002:03:00.2` | 192.0.2.25 / .26 |
| `gpu2p0` | `0010:03:00.0` | 1 | `0010:03:00.2` | 192.0.2.29 / .30 |
| `gpu3p0` | `0012:03:00.0` | 1 | `0012:03:00.2` | 192.0.2.33 / .34 |

The rail PFs carry no host networking, so later MTU changes are safe.
NUMA note: each VM's GPUs and pinned vCPUs are on NUMA node 0 — rails
`gpu2p0`/`gpu3p0` are NUMA-1 devices (cross-socket DMA, visible as ~0.8 µs
extra latency). Two links per GPU on the GPU-local socket would use the
unused port-1 PFs (out of scope for this build).

## 7.2 VF supply DaemonSet

⚙ **CUSTOM COMPONENT — `05-bf3-vf-supply.yaml` (ns `sriov-standalone`).**
*Problem:* the SR-IOV operator's policy layer reboot-loops these workers
(BF-3/CX-8 firmware-NVRAM `mlxconfig` path; see Appendix A). *Fix:* a
privileged DaemonSet that creates VFs on the selected PFs via **runtime
sysfs** (`sriov_numvfs`) and binds them to `vfio-pci`, self-healing every
15 s and re-creating VFs after node reboots — no policy layer, no reboots.
VF attributes (`trust on`, `spoof_chk off`) can be set in the same
DaemonSet; apply them as separate `ip link set` invocations (combining
both options in one call applies only the last one).

The four VFs above already exist on both workers in singleton IOMMU
groups, bound to `vfio-pci`. Verify per rail:

```bash
ls /sys/bus/pci/devices/0000:03:00.2/iommu_group/   # singleton
readlink /sys/bus/pci/devices/0000:03:00.2/driver   # .../vfio-pci
```

## 7.3 CX-8 device-plugin shims

⚙ **CUSTOM COMPONENT — `bf3-shim` source (`configs/07-rdma/bf3-shim/`) +
`08-cx8-shim-ds.yaml` (DaemonSets `cx8-shim` on tray-3, `cx8-shim-tray12`
on tray-12).** *Problems:* (1) the SR-IOV device plugin emits
`PCIDEVICE_*` env vars while KubeVirt's launcher only consumes
`PCI_RESOURCE_*` (confirmed in launcher logs and plugin source); (2) the
KubeVirt-internal plugin does not mount kernel-6.x per-device cdevs
`/dev/vfio/devices/vfioN` — upstream CNV-91802, fixed only in CNV 5.0 —
and libvirt needs them because the NVIDIA GPU plugin's cdev mounts make
the cdev API "present" in every launcher; (3) a shim serving multiple
resources from one socket unlinks peers' sockets. *Fix:* a small external
device plugin registering `openshift.io/cx8_roce_vf`, one DaemonSet per
worker, that on Allocate returns the `PCI_RESOURCE_*` env var plus
`DeviceSpec`s for the legacy group device, `/dev/vfio/vfio`, and cdevs
discovered via sysfs `vfio-dev/` (never by minor-number arithmetic —
minors are not reboot-stable). Each instance serves a **deterministic
resource-specific socket name** (SHA-256 of the resource name), so BF-3
and CX-8 shims coexist on one worker.

Build and unit tests:

```bash
cd 07-rdma/bf3-shim
go test -race ./... && go vet ./...
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags='-s -w' -o bf3-shim .
```

Deliver the binary to `/var/local/cx8-shim/device-plugin` on each worker
(delete the DaemonSet before pushing a new binary — `Text file busy`).
Implementation gotchas baked into the source: the gRPC server must
**Serve before Register** (kubelet dials back immediately); use
`DeviceSpec`, not `Mount`, for char devices (kubelet creates the node and
the cgroup device-allowlist entry).

**Exit path:** at CNV 5.0+, delete the shim DaemonSets, flip
`externalResourceProvider` to `false`, and KubeVirt's internal plugin
takes over (it contains the cdev fix).

## 7.4 Whitelist the CX-8 resource

Add the CX-8 VF to the HCO `permittedHostDevices` (same
`externalResourceProvider: true` pattern as the GPU entry in Chapter 4,
selector `15b3:101e` → `openshift.io/cx8_roce_vf`). The shim then
advertises the four VFs per worker.

## 7.5 PCI expander bus-range guard

⚙ **CUSTOM COMPONENT — `pci_bus_guard.py` (OnDefineDomain hook, stock
digest-pinned CNV sidecar-shim image; ConfigMap
`gpu-vm-test/pci-expander-bus-guard`).** *Problem:* with several
`hostDevices` attached, QEMU rejects AIE's generated topology with
`Bus 249 is already in use` — the generated PCIe-expander bus ranges
overlap (a topology-generation defect; not a BAR-capacity or hardware
limit). *Fix:* the hook recomputes each expander's required range from its
child PCI bridges and, on overlap, allocates disjoint ranges downward from
bus 255 — changing **only `target/@busNr` values**, preserving device
bindings, NUMA nodes, GPU generic initiators and MMIO sizes. Unit tests
cover 1/2/4-NIC layouts, duplicate starts, child-range overlap and valid
layouts left unchanged:

```bash
cd 07-rdma && python3 -m unittest -v test_pci_bus_guard.py
```

This is a **lab workaround**, not an upstream fix — the exact upstream
allocator defect is not identified. It is unrelated to (and must not be
confused with) the retired 4.21 MMIO-window hook, which was harmful.

```bash
oc create configmap pci-expander-bus-guard -n gpu-vm-test \
  --from-file=pci_bus_guard.py=07-rdma/pci_bus_guard.py \
  --dry-run=client -o yaml | oc apply -f -
```

## 7.6 Attach the four rails to the VMs

Two patches, applied **serially per VM** (stop one VM, patch, start,
verify guest SSH and four active NICs, then the next):

1. Baseline (hook annotation + first VF): `09-cx8-baseline.patch.yaml`
2. Four-rail device list: `12-cx8-four-rails-vm.patch.yaml` — replaces
   `hostDevices` with four `openshift.io/cx8_roce_vf` entries

```bash
# shims first — advertise all four VFs per worker:
oc patch ds cx8-shim -n sriov-standalone --type=json \
  --patch-file=07-rdma/13-cx8-four-rails-plugin.patch.json
oc patch ds cx8-shim-tray12 -n sriov-standalone --type=json \
  --patch-file=07-rdma/13-cx8-four-rails-plugin.patch.json

# then per VM (stopped):
oc patch vm gpu-vm-tray-3 -n gpu-vm-test --type=merge \
  --patch-file=07-rdma/09-cx8-baseline.patch.yaml      # first attach only
oc patch vm gpu-vm-tray-3 -n gpu-vm-test --type=merge \
  --patch-file=07-rdma/12-cx8-four-rails-vm.patch.yaml
```

Keep the guard annotation through every NIC change. Post-attach, verify
disjoint expander ranges in the live domain XML (virsh dumpxml) and four
guest NICs. Guest names shuffle between boots — map rails authoritatively
via libvirt XML (host VF source) + QMP `query-pci` (guest PCI by alias) +
guest sysfs (netdev/HCA), not by name.

**Rollback:** stop the VM, restore the two-rail or one-VF `hostDevices`
from `10-`/`09-` patch files while keeping the guard, restore the shim's
device list, and re-verify. Do not delete disks or reset workers, and do
not remove the guard while keeping a multi-NIC topology.

## 7.7 Jumbo MTU (Ethernet 9000 / RDMA 4096)

Enabled only after **empirical fabric verification** — never assume:

1. Host PFs to 9000 (via virt-handler `nsenter` — the rail PFs have no
   host networking, so this is safe):

   ```bash
   oc exec -n openshift-cnv ds/virt-handler -- nsenter -t 1 -n -- \
     ip link set dev <pf-netdev> mtu 9000
   ```

   ⚙ **CUSTOM PROCEDURE — host PF MTU via virt-handler nsenter.**
   *Problem:* the rail PFs are host devices not managed by any operator
   network config, and host CX-8 boot-time MTU provisioning is a separate
   task. *Fix:* runtime `ip link` in the host namespace through the
   virt-handler DaemonSet. **Runtime-only** — after a worker reboot the
   PFs return to 1500.

2. Guest VF netdevs to 9000 **before** adding addresses.
3. Verify: `ibv_devinfo` reports `active_mtu: 4096` on all ports (verbs
   MTU enums stop at 4096 — an RDMA MTU of 8192 does not exist), and
   **DF-set max-size pings** (`ping -M do -s 8972`) pass on all four rails
   in both directions. Had any failed, the prepared rollback restores
   1500 everywhere.

Jumbo is worth ~+7% sustained and ~+20% single-QP over MTU 1024.

## 7.8 Guest test networking (runtime-only)

Per rail, in each guest — these settings are lab-only and reset by a
guest reboot:

```bash
# keep NetworkManager from flushing manual VF IPs:
nmcli device set <cx-netdev> managed no
ip link set <cx-netdev> mtu 9000
ip addr add <ip>/<30> dev <cx-netdev>     # TEST-NET-1 probes, no gateway

# prevent ARP flux across the four interfaces (all four, per guest):
sudo sysctl -w net.ipv4.conf.<cx-netdev>.arp_ignore=1
sudo sysctl -w net.ipv4.conf.<cx-netdev>.arp_announce=2
```

⚙ **CUSTOM CONFIGURATION — per-interface ARP sysctls.** *Problem:* with
multiple VFs on one peer, Linux's default weak ARP (`arp_ignore=0`,
`arp_announce=0`) lets a reply arrive from the *wrong* VF's MAC — pings
lose packets and peer routes go ambiguous. *Fix:* strict per-interface
ARP settings on the CX-8 netdevs only (global sysctls, rp_filter and
firewall untouched).

Then discover — do not assume — the RoCEv2 GID: after the IP is set, the
IPv4-mapped GID appears (index 3 on this fabric); verify via
`ibv_devinfo` / sysfs `gid_attrs/types/* == v2` matching the IP. Verify
the peer route uses the CX-8 interface, never the virtio default.

Install guest tooling: `rdma-core libibverbs-utils perftest` (RHEL 10
BaseOS). GPUDirect testing additionally needs the source-built perftest
and `python3-pyverbs` — see [Chapter 8](08-rdma-and-gpudirect-validation.md).
