# gpu-vm-tray-3

The VM is pinned to physical worker `compute-tray-3`, using an independent
root DataVolume/PV, two `nvidia.com/31c2` GPU requests, 16 GiB RAM backed by
2 MiB hugepages, dedicated CPUs, and the AIE IOMMUFD resource.

The numbered YAML files are base provisioning manifests. Cloud-init is in
the separate `gpu-vm-tray-3-cloudinit` Secret; do not commit credentials or
copy another guest's RHSM identity. The source image is the clean RHEL4NV
ARM64 image, not another VM's registered root disk.

SSH from the bastion uses NodePort 30025:

```bash
ssh -i /home/nvidia/.ssh/hcp_rsa -p 30025 cloud-user@172.16.2.105
```

## Current tested NIC profile (2026-11-05)

The live VM uses the `pci-expander-bus-guard` hook from
[`09-cx8-baseline.patch.yaml`](../../07-rdma/09-cx8-baseline.patch.yaml),
plus the two-NIC profile in
[`10-cx8-two-rails-vm.patch.yaml`](../../07-rdma/10-cx8-two-rails-vm.patch.yaml).
It has two `openshift.io/cx8_roce_vf` hostDevices: physical VFs
`0000:03:00.2` and `0002:03:00.2` from PFs `0000:03:00.0` and
`0002:03:00.0`. No BF-3 VF is attached. These additions are separate from
the base VM manifest so the hook ConfigMap dependency is explicit.

Guest SSH and system-memory RDMA to VM12 were verified. See
[the two-rail report](../../07-rdma/two-rail-validation-2026-11-05.md) for
temporary addresses, ARP settings, test commands and limitations. VM3's guest NVIDIA/CUDA
driver installation remains a separate acceptance gate; two PCI GPUs in
the guest are not proof of CUDA or GPUDirect functionality.
