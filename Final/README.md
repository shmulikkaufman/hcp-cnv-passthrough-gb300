# Final Build Guide — HCP + OpenShift Virtualization + GB300 GPU & CX-8 RDMA Passthrough

This directory is the **consolidated, stable-state build guide**: every step
from creating the hosted OpenShift cluster to full VM-to-VM RDMA (including
GPUDirect GPU-memory transfers) between two KubeVirt virtual machines on
GB300 hardware. It contains only the **final, validated configuration**.
Everything that went wrong along the way — regressions, incidents, dead
ends and their root causes — is collected in
[Appendix A](appendix-a-issues-and-workarounds.md). Every non-standard
component (custom DaemonSet, device-plugin shim, hook, patch) is marked
where it is used with a ⚙ **CUSTOM COMPONENT** block explaining the problem
it solves; they are indexed in
[Appendix B](appendix-b-custom-components.md).

## Background

The target system is an **NVIDIA GB300 NVL72** rack: each compute tray is an
ARM64 (Grace) node with **4× GB300 (Grace-Blackwell) GPUs** (~283 GiB HBM3e
each, NVLink-interconnected pairs), **8× ConnectX-8 400 Gb/s** NICs (four
per NUMA node) and one integrated BlueField-3 ConnectX-7. The goal:

1. Run OpenShift as a **hosted (HCP) cluster** on ARM64 workers booted from
   this rack.
2. Run **OpenShift Virtualization (CNV)** and pass **two GB300 GPUs per VM**
   through to RHEL 10 guests (full NVIDIA driver + CUDA inside the guest).
3. Pass **four ConnectX-8 VFs per VM** and validate **VM-to-VM RoCE RDMA**
   — first with system memory, then **GPUDirect RDMA** (GPU-memory ↔
   GPU-memory buffers) between the two VMs.

End state (validated 2026-11-05):

| Layer | Component |
|---|---|
| Hub | OpenShift 5.0.0-rc.6, x86_64 (`hub-node-1`, 172.16.2.132) |
| Hosted cluster | OpenShift 4.22.19 HCP, Agent platform, arm64 multi-arch payload |
| Workers | `compute-tray-3`, `compute-tray-12` active; `compute-tray-11` Ready (its VM Halted) |
| Virtualization | CNV 4.22.12 **+ AIE** (NVIDIA-tuned virt-launcher, iommufd device plugin) |
| GPU operator | NVIDIA GPU Operator 26.7.2, **vm-passthrough** (sandbox) mode |
| VMs | `gpu-vm-tray-3`, `gpu-vm-tray-12`: RHEL 10.2 arm64 (64 KiB pages), 2× GB300 + 4× CX-8 VF each |
| Guest GPU stack | NVIDIA open kernel modules 615.71.10 + CUDA 13.4/13.5, `sm_103` |
| RDMA | 4 rails/VM, Ethernet MTU 9000, active RDMA MTU 4096, RoCEv2 (GID index 3) |

Validated results (details and per-test evidence in
[Chapter 8](08-rdma-and-gpudirect-validation.md)):

| Measurement | Result |
|---|---|
| Per-rail RDMA (system memory, sustained) | **390.28 Gb/s** (97.6% of 400G) |
| 4-rail concurrent one-way / full-duplex | **1,560.84 / ~2,656 Gb/s** (full line rate per rail one-way) |
| Write latency (NUMA-0 rails) | **2.34–2.38 µs** |
| GPUDirect GPU↔GPU (dma-buf), sustained | **390.25 Gb/s** per rail |
| GPUDirect 4-rail concurrent (buffers split across both GPUs) | **1,560.63 Gb/s** — full line rate |
| GPUDirect content integrity | **byte-exact**, 3 × 32 MiB verified on-GPU |
| CUDA samples in guest | 115/118 pass (2 waived by design, 1 known sample issue) |

## Infrastructure diagram (final state)

```text
                         x86_64 hub cluster — OpenShift 5.0 (172.16.2.132)
                           │  HostedControlPlane pods: my-hosted-cluster (ns: clusters)
                           │  all hosted services exposed as NodePort on the hub address
                           ▼
       ┌────────────────── ARM64 hosted (guest) cluster — OpenShift 4.22 HCP ──────────────────┐
       │                                                                                      │
       │   worker: compute-tray-3        worker: compute-tray-12        worker: compute-tray-11│
       │   (RHCOS 10.2, RHEL4NV           (same image, same tuning)      (Ready; its VM Halted) │
       │    kernel-64k 6.12.0-259.26)                                                          │
       │                                                                                      │
       │   ┌─────────────────────┐    ┌─────────────────────┐                                 │
       │   │ VM gpu-vm-tray-3    │    │ VM gpu-vm-tray-12   │                                 │
       │   │ RHEL 10.2 arm64     │    │ RHEL 10.2 arm64     │                                 │
       │   │ 64 KiB pages        │    │ 64 KiB pages        │                                 │
       │   │ 4 vCPU, 16 GiB      │    │ 4 vCPU, 16 GiB      │                                 │
       │   │ hugepages (2 MiB)   │    │ hugepages (2 MiB)   │                                 │
       │   │ dedicated CPUs      │    │ dedicated CPUs      │                                 │
       │   │                     │    │                     │                                 │
       │   │ 2× GB300 ──────── VFIO/iommufd (AIE vSMMUv3 per GPU, pcihole64 4 TiB)              │
       │   │ 4× CX-8 VF ────── vfio-pci hostDevices (openshift.io/cx8_roce_vf)                  │
       │   │ NVIDIA 615.71.10 + CUDA 13.x (guest-installed, pinned)                             │
       │   │ rdma-core + source-built perftest (--use_cuda_dmabuf)                              │
       │   └──────┬──────────────┘    └──────────┬──────────┘                                 │
       │          │ NodePort 30025 (SSH)         │ NodePort 30024 (SSH)                        │
       └──────────┼──────────────────────────────┼──────────────────────────────────────────────┘
                  │                              │
                  ▼                              ▼
      RoCE fabric (untagged isolated L2, 400 Gb/s per link, Ethernet MTU 9000)

        ┌────────────────────── RDMA rail map (per VM, all PFs 400G) ──────────────────┐
        │ rail     host PF (both workers)   NUMA   VF           guest test /30         │
        │ gpu0p0   0000:03:00.0             0      0000:03:00.2  192.0.2.20/30 (.21/.22)│
        │ gpu1p0   0002:03:00.0             0      0002:03:00.2  192.0.2.24/30 (.25/.26)│
        │ gpu2p0   0010:03:00.0             1      0010:03:00.2  192.0.2.28/30 (.29/.30)│
        │ gpu3p0   0012:03:00.0             1      0012:03:00.2  192.0.2.32/30 (.33/.34)│
        └─────────────────────────────────────────────────────────────────────────────┘
        GPUDirect: GPU0/GPU1 and all four VFs share NUMA 0 in each guest
        (NODE topology); GPU0↔GPU1 NVLink (NV18) inside each guest.
```

## Main menu

| # | Chapter | Contents |
|---|---|---|
| — | [Overview (this file)](README.md) | background, infra diagram, end-state, menu |
| 1 | [Prerequisites](01-prerequisites.md) | hardware, hub cluster, CLIs, pull secret, fabric inputs |
| 2 | [HCP hosted cluster](02-hcp-hosted-cluster.md) | hub + hosted arm64 cluster, worker boot & agent approval, NodePools, MachineConfig in HCP, host tuning, upgrades |
| 3 | [OpenShift Virtualization + AIE](03-openshift-virtualization-aie.md) | CNV operator, AIE enablement, NV launcher selection |
| 4 | [GPU operator (vm-passthrough)](04-gpu-operator-vm-passthrough.md) | ClusterPolicy, device whitelist |
| 5 | [GPU VM provisioning](05-gpu-vm-provisioning.md) | storage, VM spec (final profile), SSH services |
| 6 | [In-guest NVIDIA driver + CUDA](06-guest-nvidia-driver-cuda.md) | pinned driver install, CUDA validation |
| 7 | [CX-8 RDMA rails](07-cx8-rdma-rails.md) | VF supply, shims, 4-rail attach, jumbo MTU, guest networking |
| 8 | [RDMA & GPUDirect validation](08-rdma-and-gpudirect-validation.md) | manual SSH runbook, test suites, results, isolation proofs |
| A | [Appendix A — issues & workarounds](appendix-a-issues-and-workarounds.md) | every problem hit, symptom → cause → fix |
| B | [Appendix B — custom components](appendix-b-custom-components.md) | index of all non-standard components |
| C | [Appendix C — hub cluster setup](appendix-c-hub-setup.md) | reference: building the hub from zero (installer, MCE, storage, `hcp` CLI) |

## Where the manifests live

`configs/` inside this directory holds **only the final, working
configurations** — everything applied in the validated end state:

| Path | Contents |
|---|---|
| [`configs/01-hcp/`](configs/01-hcp/) | hosted-cluster/nodepool/infraenv references, additional NodePool example, MachineConfig wrapper example, worker host tuning |
| [`configs/02-cnv/`](configs/02-cnv/) | CNV operator manifests, AIE launcher config, HCP placement patches |
| [`configs/03-gpu-operator/`](configs/03-gpu-operator/) | operator subscription, ClusterPolicy, CNV whitelist |
| [`configs/04-vm/`](configs/04-vm/) | VM namespace, DataVolumes, VM manifests (per-tray subdirs), SSH services |
| [`configs/05-guest-driver/`](configs/05-guest-driver/) | pinned in-guest driver install script |
| [`configs/06-cuda-test/`](configs/06-cuda-test/) | CUDA driver-API probe, sample build/run scripts |
| [`configs/07-rdma/`](configs/07-rdma/) | VF supply DaemonSet, shim source + DaemonSets, PCI guard, four-rail patches, test scripts |

Superseded and historical material (retired components, two-rail-era
patches, dated validation reports, discovery evidence) lives in
`../archive/` and is never needed to rebuild the validated state.

⚠ Several files under `configs/07-rdma/` are **patches, not apply
manifests** — never bulk-apply that directory. Each chapter states exactly
which file to apply and in what order.

## Conventions used throughout

- `KUBECONFIG` points at the **hub** kubeconfig unless a step says
  "managed kubeconfig" (the hosted cluster's admin kubeconfig).
- Lab IP addresses (172.16.2.x, 192.0.2.x) are the actual addresses used in
  this environment. BMC credentials, RHSM activation keys and cloud-init
  Secrets are **never** stored in this repository; they are supplied at
  deployment time.
- The RDMA guest test networking (192.0.2.x /30s, ARP sysctls,
  NetworkManager-unmanaged state) and host PF MTU 9000 are **runtime-only**
  lab settings, not reboot-persistent production configuration; production
  IPAM/VLAN design is still pending fabric input.
