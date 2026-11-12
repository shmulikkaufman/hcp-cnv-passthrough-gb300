# Appendix B — Index of custom / non-standard components

Every place this build deviates from a stock, out-of-the-box
configuration. Each is described in context in the chapter listed; this is
the consolidated index. Components are grouped by the layer where they
live.

## B.1 Cluster configuration

| Component | Where | Problem it solves | Chapter |
|---|---|---|---|
| Worker-only placement patches | `configs/02-cnv/patches/hcp-worker-placement.yaml`, `hcp-subscription-placement.yaml` | HCP workers carry no `master` label, so CNV deployments with default master affinity stay Pending | [3](03-openshift-virtualization-aie.md) |
| KubeletConfig `max-allowable-numa-nodes: "34"` | [`vm-host-kubelet-config.yaml`](configs/01-hcp/vm-host-kubelet-config.yaml) (NodePool input) | GB300 GPUs expose many memory-only NUMA nodes; the topology manager's default 8-node limit rejects GPU-VM pods under `restricted` policy | [2](02-hcp-hosted-cluster.md) |
| Per-worker hugepage ConfigMap (64 GiB / CPU-NUMA-node) | [`vm-host-hugepages.yaml`](configs/01-hcp/vm-host-hugepages.yaml) (NodePool input) | AIE's vSMMUv3/NUMA generation requires hugepage-backed guest memory; GPU NUMA nodes must be excluded | [2](02-hcp-hosted-cluster.md) |
| `permittedHostDevices` with `externalResourceProvider: true` (GPU `10de:31c2`, CX-8 `15b3:101e`) | `configs/03-gpu-operator/04-...yaml` + HCO CR | resources are advertised by external device plugins (NVIDIA sandbox plugin, cx8-shim), not KubeVirt's internal plugin | [4](04-gpu-operator-vm-passthrough.md), [7](07-cx8-rdma-rails.md) |
| **No SR-IOV policies** (operator installed, inventory only) | cluster state | the policy layer reboot-loops these workers (Appendix A.6); its function is replaced by the VF-supply DaemonSet | [7](07-cx8-rdma-rails.md) |

## B.2 Node-side components (RDMA attach path)

| Component | Where | Problem it solves | Chapter |
|---|---|---|---|
| VF-supply DaemonSet | `configs/07-rdma/05-bf3-vf-supply.yaml` | creates VFs on the rail PFs via runtime sysfs + binds them to `vfio-pci`, self-healing; replaces the reboot-looping SR-IOV policy layer | [7](07-cx8-rdma-rails.md) |
| `bf3-shim` / cx8-shim device plugin | `configs/07-rdma/bf3-shim/` (source), `configs/07-rdma/08-cx8-shim-ds.yaml` (CX-8 DaemonSets) | bridges three KubeVirt/SR-IOV contract gaps: emits `PCI_RESOURCE_*` (launcher ignores `PCIDEVICE_*`), mounts kernel-6.x vfio cdevs (CNV-91802, fixed only in CNV 5.0), and serves each resource from a deterministic resource-specific socket so multiple shims coexist | [7](07-cx8-rdma-rails.md) |
| `pci_bus_guard.py` OnDefineDomain hook | `configs/07-rdma/pci_bus_guard.py`, `configs/07-rdma/test_pci_bus_guard.py`; ConfigMap `pci-expander-bus-guard`; patch `configs/07-rdma/09-cx8-baseline.patch.yaml` | QEMU rejects AIE-generated multi-hostdev topologies with overlapping PCIe-expander bus ranges (`Bus 249 is already in use`); the hook reallocates disjoint ranges from bus 255 touching only `target/@busNr`. Lab workaround — no upstream fix identified | [7](07-cx8-rdma-rails.md) |
| Host PF MTU via virt-handler `nsenter` | operational procedure | rail PFs are unmanaged host devices; runtime-only jumbo-MTU enablement without operator network changes | [7](07-cx8-rdma-rails.md) |
| Rail profile patches (not apply manifests) | `configs/07-rdma/09-`, `12-` VM patches; `13-` shim JSON patch (two-rail `10-`/`11-` in `../archive/07-rdma/`) | serial, guard-preserving per-VM NIC attach with shim device-list changes | [7](07-cx8-rdma-rails.md) |

## B.3 Guest-side configuration

| Component | Where | Problem it solves | Chapter |
|---|---|---|---|
| Per-interface ARP sysctls on CX-8 netdevs | guest runtime | weak default ARP lets replies arrive from the wrong rail's VF (ARP flux across the four interfaces) | [7](07-cx8-rdma-rails.md) |
| NetworkManager unmanaged on test netdevs | guest runtime | NM flushes manually-assigned VF IPs | [7](07-cx8-rdma-rails.md) |
| Source-built perftest (`--enable-cudart`) + `libperftest_kernels.so` | guest `/tmp/perftest` | RHEL 10 perftest RPM has no CUDA dmabuf support | [8](08-rdma-and-gpudirect-validation.md) |
| `python3-pyverbs` + ctypes CUDA driver API | guest | building the custom GPUDirect integrity test without a toolkit dependency | [8](08-rdma-and-gpudirect-validation.md) |

## B.4 Custom test tooling

| Component | Where | Purpose | Chapter |
|---|---|---|---|
| `rdma-vm2vm-test.sh` | `configs/07-rdma/` | self-contained system-memory RDMA validation (GID discovery, peer check, four tests, counter isolation proof, PASS/FAIL exit) | [8](08-rdma-and-gpudirect-validation.md) |
| `gpudirect-verify.py` | `configs/07-rdma/` | byte-exact GPUDirect content proof: CUDA dmabuf export + pyverbs RC QP, 3 rounds × 32 MiB positional patterns, server-side on-GPU readback verification. Encodes the versioned-symbol and iova=offset workarounds (Appendix A.8) | [8](08-rdma-and-gpudirect-validation.md) |
| `pci_bus_guard.py` unit tests | `configs/07-rdma/test_pci_bus_guard.py` | regression cover for the hook (1/2/4-NIC layouts, overlaps, valid-layout no-op) | [7](07-cx8-rdma-rails.md) |
| Bastion maintenance trees | bastion, `~nvidia/workspaces/skaufman/task_hcp/maintenance/<timestamp>-<topic>/` | per-change state backups, scripts, raw logs, counter deltas — the audit evidence referenced by every validation report | [8](08-rdma-and-gpudirect-validation.md) |

## B.5 Retired custom components (do not reuse)

| Component | Why retired |
|---|---|
| MMIO-window hook sidecar (`../archive/04-vm-legacy/gpu-mmio-hook.sh`, `-machine highmem-mmio-size=16T`) | CNV 4.21 only; on 4.22 + AIE it corrupts AIE's native PCI layout — root cause of the GPU-init regression (Appendix A.3) |
| Guest `RMForceBarPath=1` modprobe conf (`../archive/05-guest-driver/nvidia-forcebarpath.conf`) | CNV 4.21 only; on 4.22 it forces the BAR path and breaks GPU init |
| VFIO type1 loader / GPU anti-suspend DaemonSet (`../archive/03-gpu-operator/05-`, `06-`) | superseded by the iommufd resource request |
| Temporary `master` labels on HCP workers | superseded by the worker placement patches; removed |
| `../archive/07-rdma/06-sriovdp-standalone.yaml` (standalone SR-IOV device plugin) | superseded by the shims; DaemonSet deleted, manifest kept for reference |
| BF-3 VF attach to the VM pair | superseded by the CX-8 rails; BF-3 results remain historical |
