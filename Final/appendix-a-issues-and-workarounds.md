# Appendix A — Issues and workarounds (complete record)

Every problem hit while building this environment, in roughly build order:
symptom → root cause → fix. The main chapters describe only the final
state; this appendix is the history. Items marked **(resolved by design)**
no longer require action on a fresh build following the guide.

## A.1 Hosted-cluster creation

| Symptom | Root cause | Fix |
|---|---|---|
| NodePool stuck: `cluster-config-api: cannot execute binary file: Exec format error` | aarch64-only release image; control-plane pods generated on the x86_64 hub | use the **multi-arch payload** (`ocp-release:4.22.0-multi`) |
| HostedCluster rejected the base domain | `172.16.2.132.sslip.io` is an invalid DNS label | hyphenate: `172-16-2-132.sslip.io` |

## A.2 CNV / operators on HCP

| Symptom | Root cause | Fix |
|---|---|---|
| CNV operator pods Pending on the hosted cluster | HCP workers have no `master` label; deployments defaulted to master affinity | **(resolved by design)** explicit worker placement patches (`configs/02-cnv/patches/`). A temporary master-label workaround was used first and then removed — do not repeat it |
| After removing the temporary labels, some CNV deployments still pinned to masters | leftover required affinity on HCO/CNV subscription | patched HCO v1 `nodePlacements.infra` + Subscription placement, verified rollouts, then removed labels cleanly |

## A.3 GPU passthrough (the CNV 4.21 → 4.22 regression)

The dominant failure cluster of the project. On CNV 4.21 two workarounds
were required; on 4.22 + AIE **both became actively harmful** and produced
weeks of `kbusVerifyBar2_GH100: MMUTest BAR0 window … returned garbage
0x0` / `RmInitAdapter failed (0x24:0x72:1298)` / `nvidia-smi: No devices
were found` / `cuInit: 999`:

| Symptom era | Root cause | Fix |
|---|---|---|
| 4.21: `BAR 2 can't assign; no space`, `NVRM: BAR0 is 0M @ 0x0` | QEMU `virt` default 512 GiB MMIO window < ~520 GiB of GB300 BARs | 4.21 only: MMIO-window hook sidecar (`-machine highmem-mmio-size=16T`). **4.22: remove the hook** — AIE sizes `pcihole64` natively |
| 4.21: `kmemsysSetupCoherentCpuLink: Failed to get coherent GPU memory base address` | 4.21 VM lacks the Grace C2C coherent-memory ACPI description | 4.21 only: `RMForceBarPath=1` modprobe option. **4.22: remove the conf and reboot** — the NV launcher provides the description |
| 4.22 regression: garbage BAR0-window reads after upgrade | the two 4.21 workarounds carried over (hook corrupts AIE's layout; BAR-path forcing bypasses the working coherent path) | remove **both**; reboot the guest |
| `No available IOMMU models` (older launcher) | `vfio_iommu_type1` not auto-loaded on RHCOS | legacy DaemonSet, or (final) request `devices.kubevirt.io/iommufd` — the iommufd container needs no module |
| `Unknown PCI header type 127` on GPU config reads | GPU runtime-suspended while vfio-bound | `power/control=on` for vfio-bound GPUs (legacy DaemonSet) |
| `modprobe -r nvidia` hangs after a failed probe | unload hangs in this state | reboot the guest; never try to reload in place |

## A.4 VM provisioning / storage / access

| Symptom | Root cause | Fix |
|---|---|---|
| CDI upload: `open /scratch/tmpimage: permission denied` | local-hostPath dirs root-owned with `var_t` label | `chown 107:107` **and** `chcon -t container_file_t` |
| DataVolume stuck Pending: `no volume plugin matched kubernetes.io/no-provisioner` | CDI requests ~13% above nominal PVC size (filesystem overhead) | size static PVs above the PVC request |
| `virtctl image-upload` fails: `no such file or directory` | `~` not expanded in `--image-path` | pass an absolute path |
| `virtctl ssh` times out; no `virsh` exec | masquerade networking + libvirt agent allowlist (`guest-exec` disabled) | NodePort SSH service per VM |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` on SSH | NodePort previously served a different VM (new host keys) | dedicated `UserKnownHostsFile` per VM; `ssh_deletekeys: true` in cloud-init for new VMs |

## A.5 In-guest driver / CUDA

| Symptom | Root cause | Fix |
|---|---|---|
| DKMS module built for the wrong kernel | dnf pulled a newer `kernel-core` than the devel package | pin `kernel-64k-devel`/`kernel-devel-matched` to `$(uname -r)` |
| `nvidia-smi: command not found` after install | binary ships in `nvidia-driver-cuda`, not `nvidia-driver` | install `nvidia-driver-cuda-<same version>` |
| `ptxgen` sample fails (exit 16) | libNVVM sample issue | known-good failure — fails identically in NVIDIA QE's GB300 run; not a passthrough defect |
| Headless build skips GL/GLUT/Vulkan/MPI samples | no display stack | expected, not an error |

## A.6 SR-IOV / NIC passthrough

| Symptom | Root cause | Fix |
|---|---|---|
| **Node reboot loop** after applying an SR-IOV NetworkNodePolicy (~4 reboots/worker, simultaneous) | config daemon decided a reboot was needed and — with `disableDrain: true` removing serialization — rebooted all workers at once; the config never converged (`sriov_numvfs` stayed 0), re-triggering the loop | policy deleted via the hosted API; nodes stabilized next boot. **Final design: no SR-IOV policies at all** — the VF-supply DaemonSet replaces the policy layer (Appendix B). If policies are ever re-attempted on this hardware, first set `SriovOperatorConfig` `featureGates: mellanoxFirmwareReset: false`: the mellanox plugin's `mstfwreset` (PCI hot reset) is unsupported on CX7/CX8 behind PCIe switches and otherwise loops the reconcile |
| KubeVirt launcher ignores the SR-IOV plugin's device env var | plugin emits `PCIDEVICE_*`; launcher reads `PCI_RESOURCE_*` | cx8-shim emits the KubeVirt-style var |
| VF attach fails at libvirt: missing `/dev/vfio/devices/vfioN` | KubeVirt internal plugin doesn't mount kernel-6.x per-device cdevs — **CNV-91802**, fixed only in CNV 5.0 | shim's `DeviceSpec`s for group device + cdevs discovered via sysfs |
| Two shims on one worker unlink each other's sockets | single fixed socket name per resource | deterministic resource-specific socket names (SHA-256 of resource name) |
| QEMU start fails: `Bus 249 is already in use` / `Bus 250 is already in use` | generated PCIe-expander bus ranges overlap with multiple hostdevs (topology-generation defect; **not** a BAR/hardware limit — an early firmware diagnosis was wrong) | `pci_bus_guard.py` OnDefineDomain hook allocates disjoint ranges from bus 255, touching only `target/@busNr` (Appendix B). Removing the hook while keeping multi-NIC topology reintroduces the collision |
| Guest `ib_write_bw` between `rdma_cm` modes fails `rdma_resolve_addr` | not investigated; default TCP-exchange mode routes control traffic over the rail interface anyway | use default mode |
| NetworkManager flushes manually-assigned VF IPs | NM claims the netdev | `nmcli device set <dev> managed no` before assigning |
| Multi-rail pings lose packets; neighbor entry holds the **wrong rail's** MAC | default weak ARP (`arp_ignore=0`, `arp_announce=0`) lets replies cross interfaces | per-interface `arp_ignore=1`, `arp_announce=2` on CX-8 netdevs only |
| Early jumbo attempt failed while local MTU still 1500 | cannot infer fabric MTU from a failed DF ping with local MTU 1500; also RDMA MTU enums stop at 4096 | set local MTU first, then verify with DF-set 8972-byte pings per rail before trusting 9000 |

## A.7 GPUDirect RDMA

| Symptom | Root cause | Fix |
|---|---|---|
| `modprobe nvidia-peermem` fails `Invalid argument`, no dmesg trace | module needs `ib_register_peer_memory_client` — a **MOFED-only API never upstreamed**; absent from the inbox kernel's kallsyms | use the **dma-buf path** (`ibv_reg_dmabuf_mr` + `cuMemGetHandleForAddressRange`) — vendor-supported on inbox mlx5; MOFED not pursued (unconfirmed on el10nv 64 KiB-page kernel, would replace the validated stack) |
| perftest plain `--use_cuda` fails `Couldn't allocate MR with error=14` | same root cause (peermem path cannot pin GPU memory) | expected negative control; use `--use_cuda_dmabuf` |
| Host-memory `--data_validation` fails `error=12` (ENOMEM) at MR creation | validation buffer ~64 MiB vs guest memlock limit 8 MiB | run under `sudo bash -c 'ulimit -l unlimited && …'` |
| GPU-mode `--data_validation` fails: `transport retry counter exceeded (12) syndrom 0x81` | the validator's FETCH_AND_ADD **atomics on dmabuf GPU memory** are unsupported by the inbox mlx5 stack | documented limitation; content integrity proven instead by the custom `gpudirect-verify.py` |
| RHEL 10 perftest RPM has no `--use_cuda*` flags | RPM predates CUDA dmabuf support | build perftest from linux-rdma master with `--enable-cudart` (procedure in Chapter 8) |

## A.8 Custom-test development (pyverbs/ctypes pitfalls)

Encountered while writing `gpudirect-verify.py`; recorded so a re-run
doesn't rediscover them:

| Symptom | Root cause | Fix |
|---|---|---|
| `cuMemGetHandleForAddressRange` returns error 1 on a `cuMemAlloc`'d range; `cuMemcpyHtoD` segfaults | `libcuda.so.1`'s **unversioned symbols are legacy v1 stubs**; the real APIs are `cuMemAlloc_v2`/`cuMemcpyHtoD_v2`/`cuMemcpyDtoH_v2`/`cuCtxCreate_v2` (cuda.h `#define`s) | resolve versioned symbols first |
| RDMA write fails with WC status 4 (local protection error) for every remote-address candidate | pyverbs `DmaBufMR` registers **iova = offset** (MR-relative), so the GPU virtual address is outside the MR range | address both SGE and remote side as `[offset, offset+length)` |
| `TypeError: unexpected keyword argument` from `QPAttr`/`AHAttr` constructors | RHEL 10 pyverbs docstrings don't match the real init signature | construct empty, assign attributes; `AHAttr.dgid` takes a **string** |
| `SendWR` rejects a single SGE; `CQ.poll()` returns a tuple; `wc.wc_status` missing | API shape differs from docs/examples | `sg=[SGE(...)]` (a list); `n, wcs = cq.poll(k)`; use `wc.status` |
| Test processes segfault at exit (after all results printed) | pyverbs object teardown during interpreter exit / cyclic GC | `os._exit(0)` on success; `gc.disable()` for long runs |
| `pkill -f` on a guest kills your own remote shell | the pattern matches the ssh command line | use `pgrep -ax` / exact process names |
