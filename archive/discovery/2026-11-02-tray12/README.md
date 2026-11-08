# compute-tray-12 discovery and worker onboarding

Collected 2026-11-02, approximately 08:26–08:29 UTC, through the existing
`lp-nvidia` SSH connection to `rh-gb300nvl72-v2-bastion`.

**Both workers and both VMIs are now Running/Ready. Controller-managed host
tuning is complete, and the original VM has been restored and CUDA-tested. The
new guest enumerates two GB300s but still requires RHEL registration and the
NVIDIA driver/CUDA stack before GPU validation or RDMA work.**

## Latest verified checkpoint: two running VMs, 12:44 UTC

- Original VMI `gpu-vm-rhel-validation` remains Running/Ready on tray-11.
  (Renamed to `gpu-vm-tray-11` on 2026-11-02, see main README; same disk,
  same NodePort 30023, service `gpu-vm-tray-11-ssh`.)
- New VMI `gpu-vm-tray-12` is Running/Ready on tray-12. Its launcher is
  `virt-launcher-4-nv-rhel10` at the same digest as the original VM.
- New root/scratch PVs and DataVolume are independent of the source VM. The
  clean RHEL4NV image upload completed at 12:42:09 UTC with ingress-CA-verified
  TLS. The new VMI became Running/Ready at approximately 12:43 UTC.
- New guest SSH is `cloud-user@172.16.2.114` port `30024`, from the bastion.
  Its dedicated SSH trust file is in the private maintenance backup directory.
- New guest kernel is `6.12.0-259.26.el10nv.aarch64`, base page size `65536`,
  and PCI inventory sees two `10de:31c2` GPUs. Cloud-init reports Done.
- RHSM registration completed successfully at 12:53 UTC for org `11009103`;
  system name is `gpu-vm-tray-12` and status is `Registered`. Registration used
  the new guest's own identity; the source VM's subscription identity was not
  copied. The activation key was not saved in files or printed in command output.
- Matching kernel-devel, GCC, make, DKMS and NVIDIA userspace tools were absent
  at pre-registration check. These were subsequently installed, pinned to
  kernel `6.12.0-259.26.el10nv` and driver `615.71.10`; DKMS reports the module
  installed for the running kernel. CUDA Toolkit `13.4.3` was installed from
  the configured NVIDIA repository.
- Guest GPU validation passed at the driver and sample level: `nvidia-smi`
  reports two GB300s with driver `615.71.10`; `cuInit`, device enumeration,
  context creation and 256 MiB device allocation/free pass. CUDA samples were
  built for `sm_103`; `deviceQuery` reports CUDA 13.4, 2 devices and `Result =
  PASS`; `vectorAdd` completed with exit status 0. Detailed command output is
  in the private bastion file
  `/home/nvidia/workspaces/skaufman/task_hcp/maintenance/20261102T115255Z/tray12-cuda-samples.log`.
- RDMA has not been configured or tested.

## Earlier verified checkpoint: host tuning complete, 12:41 UTC

- Both workers are Ready and uncordoned, with current/desired configuration
  `38a0c867` in state Done. The NodePool reports Ready, AllMachinesReady and
  AllNodesHealthy True, with UpdatingConfig False.
- Effective kubelet `configz` was checked on both nodes: CPU Manager `static`,
  topology policy `restricted`, and `max-allowable-numa-nodes: "34"`.
- Each worker advertises four `nvidia.com/31c2` GPUs and `128Gi` of 2 MiB
  hugepages. Both run kernel `6.12.0-259.26.el10nv.aarch64+64k`.
- The original VM is Running/Ready with runStrategy Always, and its CUDA probe
  again passed two-device enumeration, context creation and GPU allocation/free.
- The host rollout itself finished around 12:25. A follow-up verification bug
  incorrectly combined `oc get --raw` with `-o json`, delaying automation. The
  raw/resource command construction was corrected and regression-tested before
  the successful live verification above. No second host rollout was performed.
- The new-VM deployment starts only after that verification. It uses the clean
  RHEL4NV image, independent node-local storage, a new guest identity and SSH
  NodePort 30024. Guest driver/CUDA setup remains a separate acceptance gate.

## Earlier maintenance checkpoint: original VM recovered, 12:22 UTC

- With restart authorization, this agent created hub ConfigMaps
  `vm-host-kubelet-config` and `vm-host-hugepages`, retained the existing OS-image
  ConfigMap reference, and appended the new references to the existing NodePool.
  InPlace `maxUnavailable` is explicitly `1`.
- Desired host tuning: static CPU Manager, restricted topology, a 34-NUMA-node
  allowance, 500m kube-reserved + 500m system-reserved CPU, and a boot-time unit
  allocating 64 GiB of 2 MiB hugepages to each of the two CPU-bearing NUMA nodes.
- Tray-12 received hardware-derived GPU/passthrough and OS discovery labels;
  its four GPUs became advertised before its tuning reboot.
- The source VM was stopped gracefully before maintenance. Its original root
  disk was preserved, with an offline XFS reflink rollback copy at
  `/var/local/kubevirt/maintenance-20261102T115255Z/root-disk.img` on tray-11.
  This same-filesystem copy is not protection against physical disk loss.
- Private configuration, effective host configuration and VM/storage backups
  are on the bastion under
  `/home/nvidia/workspaces/skaufman/task_hcp/maintenance/20261102T115255Z/`.
- Both firmware boot orders were verified to start with local Red Hat entry
  `0000`; no firmware or DHCP/PXE changes were made by this agent.
- Host SSH public keys were independently verified through authenticated
  Kubernetes node access and pinned in a dedicated `known_hosts` inside the
  private maintenance directory. The general user trust file was not changed.
- Tray-11 completed its managed update and returned Ready/uncordoned with four
  GPUs, 128 GiB of hugepages, and current/desired config `38a0c867` in state Done.
- The initial 20-minute watch expired while tray-12 was rebooting, not because
  validation rejected the configuration. At 12:21, tray-12 was still rebooting
  and the NodePool reported one of two workers updated.
- The source VM's original `Always` runStrategy was restored. Its VMI returned
  Running/Ready on tray-11, `nvidia-smi` enumerated both GB300s with driver
  `615.71.10`, and the CUDA probe passed enumeration, context creation and a
  256 MiB GPU allocation/free. The original VM is no longer intentionally Halted.
- Second-VM manifests in `04-vm/tray-12/` passed server-side validation and use
  independent local PVs, a clean RHEL4NV image, two GPUs and SSH NodePort 30024.
  Deployment is gated on both hosts completing tuning and the original VM's
  successful CUDA recovery; preparation is not proof that the new VM exists.
- No RDMA changes have been made. The existing Ubuntu PXE admission is still
  present; preserve local-disk-first boot order on any future reboot.

## Earlier checkpoint: installation complete, verified at 10:54 UTC

- Agent `cbc701dc-28cb-7870-a827-b93de3eed554` reports
  `added-to-existing-cluster`, stage `Done`, progress `100%`, and
  `Installed=True / InstallationCompleted`. Completion time reported by the Agent
  is `2026-11-02T10:53:13Z`.
- Managed node `compute-tray-12` is **Ready** with kubelet posting ready status,
  no memory/disk/PID pressure, and kubelet version `v1.36.3`.
- The node API reports RHCOS `10.2.20260828-0` and kernel
  `6.12.0-259.26.el10nv.aarch64+64k`, matching tray-11's OS/kernel baseline.
  The first-boot OS rebase to the already-configured custom image was confirmed
  by the user's `rpm-ostree status` and download logs before this successful boot.
- Existing NodePool `clusters/my-hosted-cluster` now has desired and actual
  replicas `2`; Ready, AllMachinesReady and AllNodesHealthy are True, and
  UpdatingConfig is False. No separate NodePool was required.
- No pending managed-cluster CSRs were present at verification.
- Tray-11 remains Ready with the same boot ID observed before onboarding.
- Direct automated SSH to tray-12 is still blocked by the installed OS host-key
  change. The returned ED25519 fingerprint is
  `SHA256:qnLqtVAO9NFHejbjjdZ1NueBVTAILTCngTbf7U14NzE`.
  This does not prevent cluster/API verification. No host-key trust checks were
  bypassed or trust entries modified by this agent.
- Keep the local Red Hat/Samsung M.2 boot entry ahead of Ubuntu HTTP/PXE for
  future reboots. This agent did not disable the existing Ubuntu PXE admission
  or change persistent firmware boot order. The user selected the local boot
  entry after the first reboot incorrectly chose network boot.
- The second GPU VM has **not** been created by this agent. RDMA has not been
  configured or tested.

## Earlier checkpoint: SSH monitoring at 09:18 UTC

The user booted the ISO using BMC virtual media and replaced the old discovery
SSH host-key trust entry. SSH from the bastion using
`ssh -i ~/.ssh/hcp_rsa core@compute-tray-12` now succeeds with strict host-key
verification. The earlier access/media observations below are historical, not
claims that the successful user-driven discovery boot remains blocked.

- Live discovery is booted in UEFI/ephemeral mode, with a squashfs/loop-backed
  root. Its current kernel is `5.14.0-570.58.1.el9_6.aarch64`; this is the ISO
  environment, **not** the final required RHEL4NV worker deployment.
- `agent.service` is active/running. The `next-step-runner` container is running
  and inventory logs continue to reach the Assisted Service using TLS/CA
  verification. Host management is `172.16.2.114/24` on `enP5p9s0`.
- Hub Agent `cbc701dc-28cb-7870-a827-b93de3eed554`, registered at 09:04 UTC,
  reports `Connected=True`, `Validated=True`, `approved=false`, `Bound=False`,
  `Installed=False / InstallationNotStarted`, state `known-unbound`, and no
  installation progress yet. No failed hub validation was reported.
- `NetworkManager-wait-online.service` timed out during boot, but NetworkManager
  now reports connected/full connectivity and Agent reporting/validation works.
  This timeout is not presently blocking discovery. No service was restarted or
  disabled in response.
- Reported CPU inventory: ARM64 Neoverse-V2, 144 CPUs. Physical memory:
  `1030255280128` bytes. Do not infer a NUMA-local placement policy from those
  totals alone.
- OS discovery maps the M.2 drive to `/dev/nvme4n1`, serial `S666NN0Y713614`,
  size `1920383410176` bytes, stable by-id
  `/dev/disk/by-id/nvme-eui.36363630597136140025384e00000022`.
  Four E1.S drives are `/dev/nvme0n1` through `/dev/nvme3n1`, each
  `3840755982336` bytes. Device numbering differs from tray-11; do not copy its
  install-device path. The M.2 is a candidate only, **not approved for overwrite**.
- `lsblk` reports no filesystem/mountpoint on those NVMe devices in this live
  environment. That does not prove absence of valuable data. No disk was mounted,
  formatted, wiped or selected for installation by this agent.
- The existing NodePool was last checked at 09:15 UTC and still requests one
  worker, already occupied by tray-11. No approval or NodePool changes were made.

Evidence: `ssh-discovery-monitor.json` contains the sanitized SSH/log snapshot
and hub Agent status from this checkpoint, saved mode 0600.

## Step 1: verified live baseline

- Used the existing bastion kubeconfig files without copying their contents:
  - Hub: `/home/nvidia/workspaces/skaufman/task_hcp/kubeconfig_hub`
  - Managed: `/home/nvidia/workspaces/skaufman/task_hcp/guest-kubeconfig`
- Both authenticated successfully as `system:admin`. Both contexts are called
  `admin`, so the context name alone does not identify the target cluster.
  Explicitly pass the appropriate kubeconfig.
- Hub API: `https://api.launchpad-hcp-hub.skaufman.okoyl.xyz:6443`.
- Managed API: `https://172.16.2.132:31023`.
- Hub has one Ready node, `control-plane-12`, IP `172.16.2.132`.
- HostedCluster `clusters/my-hosted-cluster` is Available and not Degraded.
  Managed cluster desired version is `4.22.19`, release image:
  `quay.io/openshift-release-dev/ocp-release@sha256:65fe37c35a6c1a8a50260790c004a4714b4f4245a4b8b23895fd6a5d7cf5e516`.
- Existing NodePool `clusters/my-hosted-cluster` has one replica, ARM64,
  Agent platform and InPlace upgrades. It reports Ready, AllMachinesReady and
  AllNodesHealthy; UpdatingConfig and UpdatingVersion are False.
- Its configuration reference is `clusters/custom-machineconfig-v2`, containing
  MachineConfig `99-worker-osimage`. The selected custom OS image is
  `quay.io/cvultur/img:5.0.0-rc.0-259.26`. The configuration has no kernelArguments,
  storage files or systemd units. Further discovery is still required for
  CPU/kubelet/hugepage settings not represented in this input.
- Managed cluster currently has only `compute-tray-11`, Ready,
  IP `172.16.2.113`. Direct host SSH confirms:
  - RHCOS `10.2.20260828-0`.
  - Kernel `6.12.0-259.26.el10nv.aarch64+64k`.
  - Base page size `65536` bytes.
  - GPU allocatable count: `nvidia.com/31c2: 4`.
  - Hugepage allocatable resource: `hugepages-2Mi: 125Gi` (this is total
    allocatable capacity, not a claim that it is all currently free).
- CNV CSV `kubevirt-hyperconverged-operator.v4.22.12` is Succeeded; HCO is
  Available and not Degraded. AIE webhook, iommufd device plugin, virt-handler,
  GPU VFIO manager and sandbox device plugin pods are Running/ready.
  HCO also reports TaintedConfiguration from its unsupported-feature annotation;
  this is not evidence of certification of the complete setup.
- VM `gpu-vm-test/gpu-vm-rhel-validation` is Running/Ready on tray-11 with
  two GB300 requests, dedicated 2-core placement, NUMA passthrough, 16 GiB guest
  memory, 2 MiB hugepages and an iommufd request. Its launcher is the AIE NV image.
- The VM explicitly reports not live-migratable because of PCI host devices.
- SSH Service `gpu-vm-rhel-validation-ssh` uses NodePort `30023`.
- Its root DataVolume request is `120Gi`; the bound PV capacity is `140Gi`.
  StorageClass `local-hostpath` has `kubernetes.io/no-provisioner`, Immediate
  binding and Retain reclamation. The root PV is pinned to tray-11. No
  VolumeSnapshotClass is configured for that storage class.
- Direct guest health check confirms RHEL kernel
  `6.12.0-259.26.el10nv.aarch64`, 64 KiB base pages, and two NVIDIA GB300 GPUs,
  each reporting driver `615.71.10` and `284208 MiB` memory.
  No CUDA workload or stress test was run in this discovery phase.

Guest SSH used the already-existing dedicated trust file
`/home/nvidia/.ssh/known_hosts_hcp_rhel_validation`. The generic known_hosts entry
for the reused NodePort is stale. Strict verification against the dedicated file
succeeded; no host keys were deleted, replaced or accepted blindly.

## Step 2: verified tray-12 addressing and access

- Host: `compute-tray-12`, IP `172.16.2.114`, management MAC
  `c4:ef:bb:1b:09:19`.
- BMC: `compute-tray-12-bmc`, IP `172.16.2.72`, MAC
  `18:3d:2d:9b:b4:a6`.
- DPU BMC: `172.16.2.52`, MAC `e0:9d:73:86:f9:a9`.
- These mappings are confirmed by bastion DNS, current DHCP reservations and
  leases, rather than inferred from an IP pattern. Older reservations also
  contain a `172.16.0.72` entry for the BMC; current DNS/lease and actual HTTPS
  response confirm the `172.16.2.72` path used here.
- HTTPS BMC web root responds `200`. The unauthenticated Redfish root responds
  `503`, identifying `AMI MegaRAC Redfish Service` and
  `Base.1.20.ServiceInUnknownState`. This is a service failure, not proof that the
  entire BMC is unreachable. No reset/restart was attempted. BMC HTTPS discovery
  used an unverified TLS context for the self-signed device certificate and sent
  no credentials.
- SSH resolves the expected hostname and matches its existing bastion host key,
  but authentication fails for `core` (including the documented `hcp_rsa` key)
  and `nvidia` using existing default keys. No passwords were guessed and no
  authorized_keys or host-key trust settings were changed.
- Tray-12 is not currently a node in either of the two inspected cluster APIs
  and has no Agent in this HostedCluster's InfraEnv. This does **not** establish
  that it is unused or safe to overwrite.
- A per-MAC Ubuntu PXE admission already exists at
  `/etc/dnsmasq.d/gb300-ubuntu-pxe-c4-ef-bb-1b-09-19.conf`, directing iPXE to
  `http://172.16.0.1:8080/ipxe/ubuntu_24_04-c4-ef-bb-1b-09-19.ipxe`.
  Do not network-boot it blindly; that target is not the HCP discovery image.
- The initial unauthenticated pass could not inventory host hardware. See the
  authenticated BMC follow-up below for subsequently recovered hardware data.
  Current OS/workloads, CPU/NUMA topology, memory, disk device paths/mounts/data
  ownership, PF/VF capabilities, IOMMU grouping, fabric/cabling and persistent
  firmware boot order remain unverified.
- No installation disk has been selected or approved for overwrite.

## Authenticated BMC follow-up

The supplied admin account authenticated successfully over IPMI 2.0 LANPlus and
the AMI web UI API. Credentials were passed in memory; neither the password nor
session tokens/cookies were written into scripts or saved evidence. The final
web inventory session was explicitly logged out. Authentication sessions and
normal audit logging aside, all hardware operations were read-only.

- System power is **On**. IPMI boot parameter 5 reports invalid boot flags and
  **No override**. Its legacy-boot wording is not evidence of the persistent
  firmware boot mode when the override flags are invalid.
- BMC firmware is `3.03.0`; reported BIOS version is `GBHC01A_01.05.0`.
- Model: Lenovo `HG635N_V2`, product `GB300_NVL72`.
- BMC system UUID: `229B9A76-C04B-11D3-02A5-A6B49B2D3D18`, distinct from
  tray-11's host UUID `35b69532-c043-11d3-02a5-e0b39b2d3d18`.
- **Identity caveat:** BMC/FRU product serial `00001S7DJVCTO1WWJ70247ZZ` is
  duplicated in tray-11's host DMI. Do not identify the target using that serial
  alone. Preserve the confirmed per-tray BMC/host MAC mappings and distinct UUIDs
  when approving any later power, boot or install operation.
- BMC PCI inventory reports **four NVIDIA `10de:31c2` GPUs**. FRU inventory
  lists two `GB300 1CPU:2GPU` boards. This does not replace OS-level enumeration
  or a functional GPU test on tray-12.
- FRU inventory identifies two **ConnectX-8 800GE 2P NIC** assemblies and one
  BlueField-3 DPU. The BMC lists four CX-8 PCI network devices (`15b3:1023`),
  each with two function entries and firmware `40.47.2526`. Actual OS PCI BDFs,
  port link modes, VF/RDMA capabilities and physical fabric paths still require
  verification.
- BMC storage inventory reports five Samsung NVMe drives:
  - `M.2_SSD`: model `SAMSUNG MZ1L21T9HCLS-00A07`, serial `S666NN0Y713614`.
  - `S1_E1.S_1`: model `SAMSUNG MZTL63T8HFLT-00AW7`, serial `S7UTNG0Y739941`.
  - `S1_E1.S_3`: same E1.S model, serial `S7UTNG0Y739923`.
  - `S0_E1.S_3`: same E1.S model, serial `S7UTNG0Y739950`.
  - `S0_E1.S_1`: same E1.S model, serial `S7UTNG0Y739925`.
  Capacity, Linux device names, partitions, mounted data and a safe install target
  are not established by this inventory. No drive is approved for overwrite.
- Processor and memory inventory endpoints returned empty arrays; no CPU count
  or memory capacity is inferred from them.
- Redfish still returns `503 / Base.1.20.ServiceInUnknownState` even with
  authentication. The failure is not resolved by credentials.
- The web UI's media endpoints are accessible, but general settings report
  `local_media_support=0`, `remote_media_support=0`, `mount_cd=0`, and
  `mount_hd=0`. Active redirections reports **No Media has been redirected**.
  The instance endpoint also returns suspicious negative port values and
  implausible session counters. Do not assume virtual media is ready or that
  these fields prove physical capacity. No enable/start/mount action was issued.

## Proposed discovery ISO delivery — not executed

1. Retrieve the **current** `status.isoDownloadURL` from the hub's ARM64 InfraEnv
   `clusters/my-hosted-cluster`; do not reuse the unrelated historical
   `oc adm node-image` day-2 image or assume a saved ISO is current.
2. Download it to a private location on the bastion with the hub ingress CA from
   ConfigMap `openshift-config-managed/default-ingress-cert`. A verified HEAD
   request already succeeds with `200`, Content-Length `103096320` bytes
   (about 98.3 MiB), and byte-range support. Its Last-Modified was
   `2026-11-02 04:43:09 GMT`. No ISO payload was downloaded in steps 1–2.
   Calculate a SHA-256 after download and retain it with the deployment evidence.
3. Preferred boot path remains tray-12 BMC web UI/KVM virtual media **only after
   resolving its disabled/unhealthy reported media state**. Admin access is now
   verified, but mounting capability is not yet validated. An SSH tunnel through
   the bastion can provide
   workstation access to the BMC UI; mounting is a later, explicitly approved
   operation. Do not reset the BMC as part of read-only inventory.
4. If virtual media needs a short remote URL rather than console file upload,
   stage a temporary copy only at an approved BMC-reachable endpoint with access
   restricted to the management environment. Confirm URL/CA/range support and
   actual BMC reachability before mounting. Do not publish the image or signed
   image-service URL broadly: discovery media and URLs can carry credentials.
5. Use a one-time virtual-CD/DVD boot only after host/disk verification and
   installation approval, then return to installed-disk boot and detach media.
   The worker must also reach the assisted services/rootfs/ignition endpoints;
   delivery of the minimal ISO alone is not sufficient.
6. If virtual media is unavailable, a separately approved fallback is to point
   **only tray-12's MAC-specific** PXE entry at this InfraEnv's ARM64 iPXE boot
   artifacts. Back up its existing Ubuntu entry, avoid rack-wide/default changes,
   and restore the intended post-install boot path. No DHCP/PXE edits are
   authorized or performed by this steps 1–2 run.

## Evidence files

- `baseline.json`: sanitized hub/managed resource exports, including NodePool,
  InfraEnv, VM/VMI, storage, CNV/AIE and GPU settings.
- `inventory.json`: addressing/reservation, SSH access and BMC probes.
- `hostchecks.json`: documented-key SSH attempt on tray-12 and direct tray-11
  host baseline.
- `followup.json`: safe MachineConfig summary, TLS-verified ISO metadata and BMC
  service responses; includes the initial generic guest trust-file failure.
- `guest-health.json`: successful guest query using the existing dedicated
  trusted-host file.
- `bmc-auth.json`: authenticated read-only IPMI controller, chassis, FRU and boot
  override data, plus authenticated Redfish failure responses.
- `bmc-ui.json`: sanitized authenticated web UI inventory and media-state GETs;
  no password, cookie or session-token values are included.
- `ssh-discovery-monitor.json`: successful live-ISO SSH checks, bounded Agent
  logs, networking, disk mapping and the hub's unapproved/unbound Agent status.

The exports are discovery evidence, **not complete restore backups**. Kubeconfig
contents, Kubernetes Secrets, cloud-init payloads, SSH public-key bodies,
last-applied annotations and signed URL credentials are excluded/redacted.
Serialized machine configuration is summarized separately rather than exported
blindly with potentially sensitive file contents. JSON files are saved mode 0600.
No VM disk or etcd backup was taken.

Next phase: inventory RDMA-capable host NICs/ports and fabric on both workers,
choose an isolated VF/PF path that preserves management networking, then plan
guest NIC attachment and system-memory verbs testing. GPUDirect is a separate
acceptance gate. Never copy the original VM's RHSM consumer identity or commit
activation keys. Before future host reboots, preserve the local-disk-first boot
path so Ubuntu PXE cannot unexpectedly reinstall the machine. Redfish repair is
separate from this worker/VM/RDMA work.
