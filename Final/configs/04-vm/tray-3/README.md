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
plus the four-NIC profile in
[`12-cx8-four-rails-vm.patch.yaml`](../../07-rdma/12-cx8-four-rails-vm.patch.yaml).
It has four `openshift.io/cx8_roce_vf` hostDevices: physical VFs
`0000:03:00.2`, `0002:03:00.2` (NUMA 0) and `0010:03:00.2`,
`0012:03:00.2` (NUMA 1). No BF-3 VF is attached. These additions are
separate from the base VM manifest so the hook ConfigMap dependency is
explicit.

Guest SSH and system-memory RDMA to VM12 were verified. See
[the two-rail report](../../../../archive/07-rdma/two-rail-validation-2026-11-05.md) for
temporary addresses, ARP settings, test commands and limitations.

## Guest NVIDIA driver/CUDA — validated 2026-11-05

Installed with the documented Phase 5 procedure
(`../../05-guest-driver/install-nvidia-driver.sh`, driver 615.71.10, kernel-devel
pinned to the running `6.12.0-259.26.el10nv`). The VM was registered with
its own RHSM identity (org `11009103`, activation key supplied in chat;
never stored). A guest-internal reboot loaded the module; the VMI kept its
UID and stayed Running throughout — no worker or VMI restart.

Verified after reboot:

- `nvidia-smi`: **2x GB300**, driver 615.71.10, CUDA UMD 13.4, both GPUs
  idle at P0 (~185 W each), 284208 MiB each.
- Both GPUs bound to the `nvidia` driver; NVRM load line present with none
  of the 4.21 failure signatures (`kmemsysSetupCoherentCpuLink`,
  `kbusVerifyBar2`, `RmInitAdapter failed`). The DVSEC3 PDI,
  `kbifCacheVFInfo` and FLA-sysmem dmesg lines are informational in a VM.
- Driver-API probe (`../../06-cuda-test/cuda-driver-probe.py`): `cuInit`
  CUDA_SUCCESS, **count: 2**, context create, 256 MiB alloc/free, PASS.
- Both CX-8 VFs survived the reboot with RDMA ports ACTIVE/LINK_UP
  (`mlx5_0`/`mlx5_1` on `enp254s0`/`enp255s0`).

The guest reboot reset the temporary RDMA test networking (test IPs, ARP
sysctls, NM-unmanaged state), as documented — re-apply it from the two-rail
report before re-running RDMA tests. Validation log on the bastion:
`maintenance/20261105T113831Z-vm3-cuda-driver/validation.log`.
