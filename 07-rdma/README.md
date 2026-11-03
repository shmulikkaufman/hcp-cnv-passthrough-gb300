# VM-to-VM RDMA on compute-tray-11 and compute-tray-12

## What is RDMA (and what this use case validates)

**RDMA (Remote Direct Memory Access)** lets a network adapter read and write
an application's memory on a remote machine *directly* — the NIC DMAs data
to/from user-space buffers with no kernel involvement, no socket buffers, no
context switches and no CPU copies on the data path. That is what gives RDMA
its two defining properties: **microsecond latency** and **line-rate
bandwidth at near-zero CPU load**, where a TCP/IP stack would spend cycles on
interrupts, protocol processing and double copies. **RoCEv2** (RDMA over
Converged Ethernet) carries the InfiniBand verbs protocol over standard
Ethernet/UDP; each RDMA port is identified by a **GID** (for RoCEv2, derived
from the interface's IP address), and standard verbs tools
(`ibv_devinfo`, perftest `ib_write_bw`/`ib_write_lat`) drive the data path.

**In this deployment**, one BlueField-3 ConnectX-7 VF is passed through to
each VM (raw PCI passthrough — the guest kernel's own `mlx5` driver provides
the verbs). The VM-to-VM validation then proves, in order:

1. **Device exposure** — the guest sees the VF (`lspci`), its driver binds,
   and the RDMA port reaches `ACTIVE` at the verbs layer.
2. **Working data path** — real RDMA verbs transfers between the two guests
   (`ib_write_bw` in both directions, `ib_write_lat`): memory on one VM is
   written remotely by the other VM's NIC.
3. **Path isolation** — the traffic demonstrably traverses *only* the BF-3
   VFs, never the virtio/masquerade management network: RDMA hardware
   counters on `mlx5_0` account for exactly the test's operations, and the
   measured bandwidth is far beyond anything virtio can carry.
4. **Performance sanity** — results in the expected range for a 400G VF
   (hundreds of Gb/s, single-digit-µs latency).

Explicitly **out of scope** here: GPUDirect RDMA (NIC DMA into GPU memory —
a separate later phase) and lossless-fabric tuning (PFC/ECN) for sustained
production loads.

Current scope is **basic system-memory RoCE RDMA** between the two Ready,
two-GB300 VMs. GPUDirect RDMA is a separate future qualification.

**Status: VM-to-VM RDMA VALIDATED (2026-11-03).** Device exposure complete
on both VMs and traffic tests pass: ~350 Gb/s single-QP, 370 Gb/s at 16 QPs,
2.4 µs write latency, isolation proven via RDMA HW counters.

## Menu

1. [Read-only NIC inventory](#read-only-nic-inventory-2026-11-02)
2. [Operator installation](#operator-installation) (+ HCP scheduling fixes)
3. [Required fabric input](#required-fabric-input-before-enabling-vfs-vm-interfaces) — still pending for production addressing
4. [Final working architecture](#final-working-architecture-2026-11-03) — gaps, deployed components, shim notes, guest validation
5. [Traffic validation](#traffic-validation-2026-11-03-rfc-5737-probe-addresses) — bandwidth, latency, isolation proof
6. [Incident record: VF policy reboot loop](#incident-record-vf-policy-triggered-node-reboot-loop-2026-11-02-resolved)

Manifests in this directory: `00`–`03` SR-IOV operator setup ·
`04` node policy (⚠️ incident, do not apply) · `05` VF supply DaemonSet ·
`06` standalone sriov plugin (superseded) · `07` shim DaemonSet ·
`bf3-shim/` shim source · `rdma-vm2vm-test.sh` self-contained VM-to-VM RDMA
test (server + client modes; validated 2026-11-03, all tests pass).

## Read-only NIC inventory (2026-11-02)

- Both trays have active RoCE device `mlx5_16` (tray-11) / `mlx5_8` (tray-12),
  each the BlueField-3 integrated ConnectX-7 (`15b3:a2dc`) PF at BDF
  `0016:01:00.0`, NUMA node 1, `400 Gb/s (4x NDR)`, firmware `32.47.2682`.
- The PF interface is `enP22s22np0`; its host link is up, but it has no IP
  address. Tray-11 management remains on `enP5p9s0`; the selected RDMA PF is not
  the management interface.
- Each BlueField PF reports 16 VFs available, zero currently enabled, and a
  one-device IOMMU group. PCI isolation is promising but still needs validation
  after VF creation.
- Tray-11 CX-8 (`15b3:1023`) ports currently report no carrier. Tray-12 CX-8
  ports are link-up, but that does not establish corresponding connected CX-8
  links on tray-11. This plan therefore targets the active BF3 RoCE link on both
  workers; do not reassign or bind a CX-8 function based on model alone.
- The existing GPU VM and both worker management networks are separate from the
  target RDMA PFs. Keep management networking untouched.
- No `ibv_devinfo`/perftest tools or SR-IOV/NVIDIA Network Operator CRDs were
  installed in the managed cluster at initial inventory.

## Operator installation

The first three manifests install the OpenShift **4.22 stable SR-IOV Network
Operator** in its own namespace. The subscription is pinned to the catalog's
current CSV at discovery time (`v4.22.0-202610241138`). Installation provides
inventory/controllers; it does **not** itself enable VFs or attach NICs to VMs.

Install with the managed-cluster kubeconfig, then wait for the CSV and operator
pods to be healthy. Check the generated `SriovOperatorConfig` and
`SriovNetworkNodeState` on both workers before defining any policies.

### Hosted-cluster scheduling fixes (applied 2026-11-02)

Two fixes were required because this guest cluster is a hosted control plane
with worker-only nodes:

1. The CSV deployment hard-requires `node-role.kubernetes.io/master`, which
   does not exist in the guest cluster, so the operator pod stayed Pending and
   the CSV timed out to `Failed`. Fix (user-approved): both workers were
   labeled `node-role.kubernetes.io/master=` so the pod schedules. Rollback is
   `oc label node <node> node-role.kubernetes.io/master-`. The subscription
   also carries the documented HCP `spec.config.nodeSelector`
   (`node-role.kubernetes.io/worker: ""`) as belt-and-suspenders; see
   "Deploy the SR-IOV Operator for hosted control planes" in the 4.22 docs.
2. The OLM subscription does not create the default `SriovOperatorConfig`,
   which the CVO normally provides; without it the operator logs "default
   SriovOperatorConfig object not found" and stays idle.
   `03-sriov-operatorconfig.yaml` creates it with `disableDrain: true`
   (both workers run non-migratable GPU VMs; config changes must not drain
   them) and a worker `configDaemonNodeSelector`.

After these fixes, all operator workloads are Running on both workers
(config-daemon 2/2, injector 2/2, webhook 2/2) and both
`SriovNetworkNodeState`s report `Succeeded`. The operator independently
confirmed the BlueField-3 PF inventory on both nodes: `enP22s22np0` at
`0016:01:00.0`, `15b3:a2dc`, 400 Gb/s ETH link up, `totalvfs: 16`,
`eSwitchMode: legacy`, MTU 1500, driver `mlx5_core`.

## Required fabric input before enabling VFs/VM interfaces

Obtain the approved RDMA guest network configuration:

- VLAN/tagging mode (or confirmation that the active BF3 port is an untagged
  isolated L2 fabric).
- Two reserved guest IPv4 addresses and subnet/prefix, or approved DHCP service.
- Gateway only if guests need off-subnet connectivity; perftest on a dedicated
  same-L2 subnet usually needs none.
- RoCE GID selection and MTU expected on this fabric, plus confirmation that the
  two BF3 ports connect to the same L2/RoCE fabric.

Do **not** guess a private subnet, VLAN, GID index or MTU. Host MTU is currently
1500; that is not permission to change the NIC or the switch to jumbo frames.

## Final working architecture (2026-11-03)

```text
                     ┌───────────────────────────────────────────────┐
                     │      RoCE fabric (untagged L2 · 400 Gb/s)     │
                     │      shared segment (mgmt-net ARP visible)    │
                     └────────▲──────────────────────────▲───────────┘
                              │                          │
      ════════ RDMA data path (verbs, GID ::ffff:192.0.2.x) ════════
      ════════ measured: 350 Gb/s @1QP · 370 Gb/s @16QP · 2.4 µs ════
                              │                          │
┌─────────────────────────────┴─────────┐ ┌──────────────┴──────────────────┐
│  VM gpu-vm-tray-11   (RHEL 10, arm64) │ │  VM gpu-vm-tray-12  (RHEL 10)   │
│ ┌───────────────────────────────────┐ │ │ ┌─────────────────────────────┐ │
│ │ eth0 10.0.2.2  virtio masquerade  │ │ │ │ eth0 10.0.2.2 (same)       │ │
│ │      └─ mgmt/SSH only, no RDMA    │ │ │ │                             │ │
│ │ enp10s0 192.0.2.11/24 (probe IP)  │ │ │ │ enp10s0 192.0.2.12/24      │ │
│ │      └─ mlx5_0 · RDMA port ACTIVE │ │ │ │      └─ mlx5_0 · ACTIVE    │ │
│ │ 2x GB300 → nvidia 615.71.10       │ │ │ │ 2x GB300 → nvidia 615.71.10│ │
│ │ rdma-core + perftest              │ │ │ │ rdma-core + perftest       │ │
│ └───────────────────────────────────┘ │ │ └─────────────────────────────┘ │
└────────┬──────────────────────┬───────┘ └───────┬──────────────┬──────────┘
         │ KubeVirt hostDevices │ SSH NodePort    │ same          │ SSH NodePort
         │ (vfio-pci <hostdev>) │ 30023           │               │ 30024
┌────────┴──────────────────────┴───────┐ ┌───────┴──────────────┴──────────┐
│ compute-tray-11  (RHCOS 10, arm64)    │ │ compute-tray-12 (RHCOS 10)      │
│                                       │ │                                  │
│  BF-3 PF 0016:01:00.0 (enP22s22np0)   │ │  BF-3 PF 0016:01:00.0 (same)    │
│    └─ VF 0016:01:00.1 [vfio-pci]      │ │    └─ VF 0016:01:00.1           │
│         vf 0: trust on, spoof off     │ │         vf 0: trust on, spoof…  │
│  2x GB300 → nvgrace_gpu_vfio_pci      │ │  2x GB300 → nvgrace_gpu_vfio…   │
│                                       │ │                                  │
│  ── sriov-standalone ns DaemonSets ── │ │  (identical, one per node)      │
│  • bf3-vf-supply: create VF via sysfs │ │                                  │
│    + vfio-pci bind + VF attributes    │ │                                  │
│  • bf3-shim: kubelet device plugin    │ │                                  │
│    → PCI_RESOURCE_* env (KubeVirt)    │ │                                  │
│    → /dev/vfio cdev DeviceSpecs       │ │                                  │
│    → advertises openshift.io/         │ │                                  │
│      mellanox_bf3roce_vf (1 device)   │ │                                  │
└───────────────────────────────────────┘ └──────────────────────────────────┘

  glue (cluster-wide): HCO permittedHostDevices: 15b3:101e →
    openshift.io/mellanox_bf3roce_vf, externalResourceProvider: true
    (GPU entry nvidia.com/31c2 is the model — same pattern, NVIDIA plugin)
  SR-IOV operator: installed, inventory only — deliberately NO policies
    (its policy layer reboot-loops this BF-3; see incident record)
  bastion → NodePort 30023/30024 → guest eth0 = management plane (kept
    separate from the RDMA data plane by construction and by counters)
```

The VF is passed through to **both** VMs and the guest RDMA ports are ACTIVE.
The stock SR-IOV operator path cannot be used end-to-end on this platform;
three gaps, each bridged:

| Gap (stock component) | Bridge |
|---|---|
| SR-IOV policy layer reboots nodes on the BF-3 (firmware NVRAM / `mlxconfig` path; manual runtime sysfs works fine) | `05-bf3-vf-supply.yaml`: runtime sysfs VF creation + vfio-pci bind DaemonSet |
| SR-IOV device plugin emits `PCIDEVICE_*` env vars; KubeVirt's launcher only reads `PCI_RESOURCE_*` (confirmed by launcher logs and plugin source `pool_stub.go`) | `07-bf3-shim-ds.yaml` + `bf3-shim/`: external device plugin emitting the KubeVirt-style env var |
| KubeVirt internal plugin (`externalResourceProvider: false`) does not mount kernel-6.x per-device cdev `/dev/vfio/devices/vfioN` — **CNV-91802**, fixed only in CNV 5.0 (KubeVirt v1.9, PR #18300); libvirt needs it because the NVIDIA GPU plugin's cdev mounts make the cdev API "present" in every launcher | Same shim: `DeviceSpec`s for `/dev/vfio/<group>`, `/dev/vfio/vfio`, and cdevs discovered via sysfs `vfio-dev/` (re-read at every Allocate; minors are not reboot-stable) |

Architectural reference: the user's validated x86 project
`github.com/shmulikkaufman/nvd-launchpad-perf` (same `hostDevices` +
`permittedHostDevices` pattern; on its kernel 5.14 the internal plugin suffices
because the vfio cdev API does not exist there).

Required config: `permittedHostDevices` entry for `15b3:101e` →
`openshift.io/mellanox_bf3roce_vf` with **`externalResourceProvider: true`**
on the HCO CR (`spec.virtualization.permittedHostDevices`, propagating to the
KubeVirt CR's `spec.configuration.permittedHostDevices`). The GPUs'
`nvidia.com/31c2` entry is the model (NVIDIA's plugin emits `PCI_RESOURCE_*`
and mounts cdevs — the shim replicates that behavior for the NIC VF).

### Deployed components

- `05-bf3-vf-supply.yaml` (ns `sriov-standalone`): privileged DaemonSet that
  creates 1 VF on PF `0016:01:00.0` via hostPath `/sys` and binds it to
  `vfio-pci`; self-heals every 15s and re-creates the VF after node reboots.
- `bf3-shim` (from `bf3-shim/`, built with
  `CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build`, delivered to
  `/var/local/bf3-shim/bf3-shim` on both nodes): device plugin registering
  `openshift.io/mellanox_bf3roce_vf`, advertising device `0016:01:00.1`
  (health = driver still `vfio-pci`). Allocate returns the
  `PCI_RESOURCE_*` env var plus DeviceSpecs (`mrw`) for the legacy group
  device, `/dev/vfio/vfio`, and `/dev/vfio/devices/vfioN`.
- `06-sriovdp-standalone.yaml` is **superseded** by the shim (its DaemonSet
  was deleted); kept for reference.
- The SR-IOV operator itself is still installed and healthy but only provides
  inventory; **no policies exist** (deliberately — see incident record).

### Shim implementation notes (learned the hard way)

- The gRPC server **must Serve before Register**: kubelet dials back to the
  plugin socket for ListAndWatch immediately after Register() returns;
  registering first causes `failed to dial device plugin: context deadline
  exceeded` on every retry.
- Use `DeviceSpec` (not `Mount`) for char devices: kubelet creates the node
  in the container AND adds cgroup device-controller allowlist entries.
- Discover cdev names from `/sys/bus/pci/devices/<BDF>/vfio-dev/` (verified
  present on this kernel), never by minor-number arithmetic.
- Binary redeploy sequence: delete the DaemonSet (avoids `Text file busy`),
  push the base64'd binary via virt-handler nsenter to
  `/var/local/bf3-shim/bf3-shim`, re-apply the DaemonSet.

### Exit path

At CNV 5.0+: delete the shim DaemonSet, flip `externalResourceProvider` to
`false`, and KubeVirt's internal plugin takes over (it contains the cdev fix,
PR #18300). A 4.22.z backport request could also be filed via Red Hat support
referencing CNV-91802.

### Guest validation (2026-11-03, both VMs)

Identical results in `gpu-vm-tray-11` and `gpu-vm-tray-12` (tray-12 restarted
~13:04 UTC; clean `Pending → Scheduled → Running`, ~35s):

```
lspci:   0a:00.0 Ethernet controller [0200]: Mellanox Technologies
         ConnectX Family mlx5Gen Virtual Function [15b3:101e] (rev 01)
driver:  mlx5_core (guest kernel)
netdev:  enp10s0 UP
rdma:    link mlx5_0/1 state ACTIVE physical_state LINK_UP netdev enp10s0
GPUs:    2x NVIDIA GB300, driver 615.71.10 (unaffected)
```

Device exposure is COMPLETE on both VMs. Remaining work for traffic tests:

1. Guest tooling: install `rdma-core` + `perftest` in both guests.
2. Fabric addressing on the VF netdevs (`enp10s0`, currently no IPs; RoCEv2
   GIDs derive from IPs — see the fabric input section above, still pending).
   Per the LaunchPad GB300 RoCE guide (`rdma-gb300.html`): use static unique
   IPs per interface, resolve the RoCEv2 GID index by filtering
   `gid_attrs/types/* == v2` and matching the IP, and set VF attributes
   `trust on` / `spoof off` (can be added to the supply DaemonSet).
3. Bidirectional `ib_write_bw` (system memory) between the guests.
4. GPUDirect RDMA (`--use_cuda_dmabuf`, no peermem needed with the open
   driver) as a separate later phase.

### Traffic validation (2026-11-03, RFC-5737 probe addresses)

Setup: VF attributes `trust on` + `spoof_chk off` set on both host PFs
(supply DaemonSet, hostNetwork; note: combining both options in one
`ip link set` applies only the last one — use separate invocations).
`rdma-core` + `libibverbs-utils` + `perftest` installed in both guests
(all in RHEL 10 BaseOS). Throwaway probe IPs (RFC-5737 TEST-NET-1, safe:
RoCEv2 only uses the IP for GID derivation + ARP):
`192.0.2.11/24` (tray-11), `192.0.2.12/24` (tray-12).

**Guest gotcha:** NetworkManager may flush manually-assigned VF IPs (seen on
tray-11: `nmcli device set enp10s0 managed no` first, then the IP persists;
tray-12 kept it as "connected (externally)"). The fabric passes untagged L2
between the trays (ping 5/5, 0.15 ms RTT, ARP resolves; the BF-3 fabric is a
shared L2 — other hosts' 172.16.2.x ARP chatter is visible on it).

RoCEv2 GID appears at index 3 after IP assignment (0/1 = link-local v1/v2,
2/3 = IP-derived v1/v2). Results (`ib_write_bw -d mlx5_0 -x 3 -F
--report_gbits`, MTU 1024, 64 KiB messages):

| Test | Result |
|---|---|
| tray-12 → tray-11, 1 QP, 5000 iters | **350.09 Gb/s** avg (351.40 peak) |
| tray-11 → tray-12, 1 QP, 5000 iters | **350.19 Gb/s** — symmetric |
| tray-12 → tray-11, 16 QPs, 30 s | **369.96 Gb/s** |
| `ib_write_lat` 2 B, 1000 iters | **2.38 µs min / 2.44 µs avg** (99%: 2.74 µs) |

Isolation proof: `rdma statistic show link mlx5_0/1` on the server shows
`rx_write_requests = 5000` (exactly the iteration count) — RDMA hardware
counters, not netdev counters (verbs traffic bypasses netdev stats).
Bandwidth magnitude alone excludes the virtio/masquerade path. The `-R`
(rdma_cm) perftest mode failed with `rdma_resolve_addr` (not investigated —
the default TCP-exchange mode routes its control traffic over `enp10s0`
anyway, since `192.0.2.0/24` only exists there).

### Repeatable test script

`rdma-vm2vm-test.sh` (in this directory; also deployed to both guests at
`/tmp/rdma-vm2vm-test.sh`) wraps the whole validation: GID auto-discovery,
peer reachability, four tests, counter-based isolation proof, PASS/FAIL
summary and exit code. Usage:

```bash
# on one VM:
sudo ./rdma-vm2vm-test.sh server
# on the other VM:
sudo ./rdma-vm2vm-test.sh <server-VF-IP>
```

Validated results (2026-11-03): 351 Gb/s @1 QP · 370 Gb/s @16 QPs ·
690 Gb/s bidirectional (aggregate) · 2.44 µs write latency ·
`rx_write_requests` advanced exactly by the test operations. Note: the
16-QP test requires `-q 16` on the server listener too (perftest
pre-creates QPs at startup; message size/iters/bidirectional are exchanged
and adapted, QP count is not).

Probe IPs are runtime-only (not persisted across guest reboots). Production
addressing on the BF-3 fabric still needs approved VLAN/subnet input; with a
real VLAN, add `ip link set enP22s22np0 vf 0 vlan <id>` to the supply
DaemonSet.

Remaining: GPUDirect RDMA (`--use_cuda_dmabuf`) as a later phase; optional
PFC/ECN lossless config for sustained production loads (not needed for
validation — no loss observed).

## Incident record: VF policy triggered node reboot loop (2026-11-02, resolved)

Applying `04-sriov-nodepolicy.yaml` (1 VF on `0016:01:00.0`, `deviceType:
netdevice`, `isRdma: true`) caused both workers to reboot **simultaneously and
repeatedly** (~4 reboots each). Journal evidence from both nodes shows this was
**not a kernel panic**:

```
systemd[1]: Started sriov-network-config-daemon-reboot.service - sriov-network-config-daemon reboot node.
```

The config daemon decided the config required a reboot to apply; because
`disableDrain: true` was set in the operator config, it rebooted immediately and
on both nodes at once instead of draining them serially. After each boot the
config did not converge (VFs were never created: `sriov_numvfs` stayed 0), so
the daemon re-triggered the reboot, producing the loop.

**Remediation:** the policy was deleted via the (hosted, still-reachable)
managed API. Since VFs do not persist across reboots, the next boot had nothing
to apply and both nodes stabilized within ~15 minutes. Verified recovered:
nodes Ready, both VMIs Running, both guests see 2x GB300 with driver 615.71.10,
`sriov_numvfs=0`, node states `Succeeded` with no errors.

**Lessons / revised constraints:**

- `disableDrain: true` prevents VM eviction but removes the operator's
  serialization; a config that triggers reboots then hits all workers at once.
- The netdevice+isRdma VF config on this BlueField-3 PF did not converge and
  required reboots; do not re-apply the same policy as-is.
- Do not re-attempt VF policy changes without either (a) enabling drain again
  (accepting VM restarts, serialized one node at a time), or (b) a policy
  variant that avoids the reboot path (e.g. `deviceType: vfio-pci` with
  `isRdma: false`, which the webhook permits and which leaves driver binding
  entirely to the guest).
- Investigate config-daemon apply logs from the incident boots before any
  retry; the reason the config never converged is still unknown.

> **Superseded plan:** the numbered plan that originally followed this
> section (create a `SriovNetworkNodePolicy`, let the operator create VFs,
> attach via Multus/SriovNetwork) was abandoned after the incident and after
> discovering the env-var and cdev gaps documented above. The current
> architecture replaces the operator's policy layer with `05-bf3-vf-supply`
> and the operator's device plugin with the `bf3-shim`; see "Final working
> architecture" and "Remaining work" above. The Multus/`SriovNetwork` step is
> no longer needed — the VFs attach to the VMs directly as `hostDevices`.

The 4.22 SR-IOV operator installation is a cluster change; watch the running
GPU VMs and NodePool health during maintenance. Do not delete the original
root disk/PV or revert the completed kubelet/hugepage tuning as an RDMA
rollback.
