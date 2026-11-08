# 1 — Prerequisites

## Hardware

- One or more **GB300 NVL72 compute trays** (ARM64 Grace CPU, 4× GB300
  GPUs, 8× ConnectX-8 400G — four per NUMA node — plus an integrated
  BlueField-3). This build used three trays: `tray-3`, `tray-11`,
  `tray-12`.
- The CX-8 ports used for RDMA must connect to an **isolated L2 RoCE
  fabric** shared between the workers (untagged in this build). Verify with
  link state and peer ARP before relying on it.
- BMC/ILOM access to each tray for ISO boot and power control (credentials
  supplied out-of-band; never committed).

## Hub cluster

An existing x86_64 OpenShift 5.0 cluster with:

- `multicluster-engine` installed (standalone MCE is sufficient; ACM
  optional).
- A default `StorageClass` for the hosted etcd (here: `lvms-vg1` from the
  LVMS operator; any RWO block storage works).
- A load-balancer story for on-prem: this build exposes all hosted-cluster
  services as **NodePort on the hub address** (172.16.2.132), so MetalLB is
  not required.

If you do not have a hub yet, [Appendix C](appendix-c-hub-setup.md) is the
reference procedure for building one (installer, MCE + Hosted Control
Planes, storage, `hcp` CLI) — note it was not re-validated in this build.

```bash
export KUBECONFIG=$PWD/kubeconfig_hub   # hub kubeconfig
```

## Workstation / bastion tooling

- `hcp` (HyperShift) CLI matching the hub version.
- `oc`, `virtctl`.
- Pull secret (`pull-secret.json`).
- SSH keypair for node access (`~/.ssh/hcp_rsa` / `.pub`).
- The guest OS image: **RHEL4NV ARM64 KVM image**
  (`rhel4nv-26.03-aarch64-kvm.qcow2`, kernel `6.12.0-259.26.el10nv`,
  64 KiB base pages). Keep this clean image — new VMs must be built from
  it, never from another guest's registered root disk.

## Required fabric input (before production RDMA addressing)

Obtain from the network team before designing real guest addressing:

- VLAN/tagging mode (this build's fabric is untagged isolated L2).
- Reserved guest IPv4 subnets per rail, or approved DHCP.
- Expected MTU on the fabric (this build verified **9000 empirically** with
  DF-set pings before enabling it; do not assume).
- Confirmation that the workers' RDMA ports share one L2/RoCE segment.

**Do not guess** subnets, VLAN, GID index or MTU. The validation in
[Chapter 8](08-rdma-and-gpudirect-validation.md) uses throwaway RFC-5737
TEST-NET-1 addresses (192.0.2.x) purely as lab probes; RoCEv2 only uses the
IP for GID derivation and ARP.

Additionally, production RDMA under load requires a **lossless Ethernet
fabric** — PFC/ECN (or an adaptive-routing fabric such as Spectrum-X) on
the switches. The validation fabric in this build is best-effort (no loss
observed at line rate, congestion untested); obtain the switch-side
congestion-management story along with the addressing inputs above.

## Red Hat entitlements (per guest VM)

Each guest needs its **own** RHSM identity: org ID + activation key with
access to:

- `rhel-10-for-aarch64-nvidia-rpms` (RHEL for NVIDIA driver/kernel channel)
- `codeready-builder-for-rhel-10-aarch64-rpms`

Never copy a consumer identity between guests and never commit activation
keys. The public NVIDIA CUDA repo
(`https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/`)
needs no entitlement.

## Readiness check

Hub gaps to verify before starting: MCE sufficient, CNV on the hub only
needed if workers are VMs (not the case here), MetalLB avoided via
NodePort, default StorageClass mandatory.
