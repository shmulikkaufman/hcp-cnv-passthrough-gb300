# Second GPU VM on compute-tray-12

Target: `gpu-vm-test/gpu-vm-tray-12`, two GB300 GPUs, two dedicated vCPUs,
16 GiB guest RAM, 2 MiB hugepages, and AIE/iommufd. GPU launchers are selected
by the existing AIE resource rule. No legacy MMIO sidecar or RMForceBarPath
workaround is used.

## Deployed status, 2026-11-02 12:44 UTC

- VMI Running/Ready on `compute-tray-12`, using the same AIE NV launcher image
  digest as the working tray-11 VM.
- Independent root DataVolume upload Succeeded; root/scratch PVs are local to
  tray-12. The source VM's disks were not reused.
- Guest SSH: `ssh -p 30024 cloud-user@172.16.2.114` from the bastion.
- Guest OS is RHEL 10.2, kernel `6.12.0-259.26.el10nv.aarch64`, base page size
  `65536` bytes. Guest PCI inventory enumerates two GB300s at `fd:00.0` and
  `ff:00.0`. Cloud-init completed.
- RHSM registration completed successfully at 12:53 UTC for org `11009103`;
  the registered system name is `gpu-vm-tray-12`, and subscription-manager reports
  `Overall Status: Registered`. Registration used this guest's own identity; the
  original VM's consumer identity was not copied.
- Kernel-devel, GCC, make, DKMS and `nvidia-driver-cuda` were absent immediately
  before registration. Guest NVIDIA driver/CUDA installation and testing remain
  outstanding. PCI enumeration alone is not GPU acceptance.

- The original VM remains Running/Ready on tray-11 and passed its CUDA driver
  probe after managed host maintenance. No RDMA setup has been applied.

## Prerequisites and deployment order

1. Complete the managed host tuning described in
   `../../01-hcp/vm-host-tuning.md` (repository-relative path:
   `01-hcp/vm-host-tuning.md`). Verify tray-12 has static CPU Manager,
   restricted topology with the 34-node allowance, sufficient NUMA-local
   hugepages, four advertised `nvidia.com/31c2` GPUs and an iommufd resource.
   Restore and regression-test the original VM before booting this one.
2. Confirm namespace `gpu-vm-test` exists, NodePort 30024 is unused, and these
   resource names/backing directories do not belong to unrelated work.
3. Create `/var/local/kubevirt/gpu-vm-tray-12-root` and
   `/var/local/kubevirt/gpu-vm-tray-12-scratch` on tray-12. Set owner `107:107`
   and SELinux type `container_file_t`. Do not format or reuse a block device.
4. Apply `00-local-pvs.yaml`, then `01-root-dv.yaml`, with the **managed**
   kubeconfig. The static PVs reserve the exact destination PVC names and are
   pinned to tray-12. Retain the original tray-11 storage unchanged.
5. Upload the existing clean image from the bastion's absolute path:
   `/home/nvidia/workspaces/skaufman/task_hcp/hcp_cnv/rhel4nv-26.03-aarch64-kvm.qcow2`.
   Wait for the DataVolume to reach Succeeded before starting the VM. Verify
   the managed cluster's CDI upload route certificate using its ingress CA
   rather than silently disabling TLS verification.
6. Create Secret `gpu-vm-tray-12-cloudinit` with a `userdata` key containing
   cloud-config. Use hostname `gpu-vm-tray-12`, the intended SSH **public** keys,
   and `ssh_deletekeys: true` so the new guest has its own host identity. Do not
   copy private keys, source guest identity, RHSM consumer certificates or a
   live root disk. Do not commit credential-bearing user-data to this repository.
7. Apply `02-gpu-vm.yaml` and `03-ssh-service.yaml`. KubeVirt generates fresh
   MAC/firmware identifiers; no source VM identifiers are copied.
8. Check VMI Running/Ready on tray-12, the NV AIE launcher, two guest PCI GPUs,
   the documented guest kernel and 64 KiB base-page size. SSH is:
   `ssh -p 30024 cloud-user@172.16.2.114`.
9. The clean image still needs the documented guest NVIDIA driver/CUDA setup
   (`05-guest-driver/` and README Phase 5), with fresh RHEL entitlement if
   required. Never copy the original VM's subscription identity. Keep kernel,
   kernel-devel, open kmod and userspace versions matched.

Do not call the second VM GPU-ready until `nvidia-smi`, the driver-API probe,
deviceQuery and vectorAdd pass. Enabling RDMA is a subsequent phase requiring
dedicated NIC/VF resources on **both** VMs and fabric validation.

## RDMA VF passthrough (2026-11-03)

The VM now carries one BlueField-3 ConnectX-7 VF (`15b3:101e`) via KubeVirt
`hostDevices` (resource `openshift.io/mellanox_bf3roce_vf`), attached when the
VM was restarted ~13:04 UTC after the supply/shim stack was deployed — see
`../../07-rdma/README.md` for the full architecture. Validated in the guest:

```
lspci:   0a:00.0 Mellanox ConnectX Family mlx5Gen Virtual Function [15b3:101e]
driver:  mlx5_core (guest kernel, inbox)
netdev:  enp10s0 UP (no IP yet — awaiting fabric details)
rdma:    link mlx5_0/1 state ACTIVE physical_state LINK_UP
GPUs:    2x GB300, driver 615.71.10 — unaffected
```

The VF has no guest IP until the RoCE fabric VLAN/subnet is confirmed;
traffic tests (`ib_write_bw`) remain pending on that input.

## Recovery

The source VM, root PVC/PV and offline rollback copy are independent of this
deployment. Do not delete/reassign them to fix the new VM. On a new-VM failure,
halt only `gpu-vm-tray-12` and inspect events/guest logs; preserve its root disk
unless deletion is separately approved. This storage class has no snapshot
support and no dynamic provisioning.
