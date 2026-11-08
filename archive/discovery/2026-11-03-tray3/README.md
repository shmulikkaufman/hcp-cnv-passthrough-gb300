# tray-3 onboarding to my-hosted-cluster — resume checkpoint (2026-11-04)

The session was suspended before the BMC boot trigger on 2026-11-03.
Onboarding resumed on 2026-11-04 after worker-only scheduling was fixed and
the user supplied corrected BMC credentials. **Tray-3's OS installation has
started; do not repeat the discovery/reinstall procedure below.** The latest
checkpoint is initial-OS boot recovery followed by the normal first-boot OS
update. Node Ready/target-kernel validation is still pending.

## Latest checkpoint (2026-11-04)

- CNV infrastructure and its OLM Subscription now explicitly select workers.
  The temporary Kubernetes `master` labels were removed from tray-11/12 only
  after the CNV rollouts completed. Both nodes report `Ready worker`.
- Both VMs remained Running/Ready with unchanged instance UIDs and worker
  boot IDs. GPU and BF-3 resources remain advertised.
- Our NodePool now desires three workers; two are Ready so far. Jose's
  NodePool remains 1/1 (tray-8).
- Tray-3 Agent `clusters/ad0d333e-a8a3-13ce-d249-b205349c3ae2` appeared at
  approximately 08:54 UTC. Its ARM64 inventory, management MAC, product
  serial, and OS SSD serial matched. It is approved and bound to
  `my-hosted-cluster`, with `spec.role: worker` and
  `spec.installation_disk_id` set to the verified 1.92 TB SSD's stable ID.
- The discovery entrypoint was restored to `exit` **before** approval and
  NodePool scale-up. No tray-8 entrypoint was changed.
- All six staged artifact URLs still return HTTP 206; our InfraEnv reports
  ImageCreated=True.
- Earlier BMC authentication failures were resolved using corrected
  user-supplied credentials (not saved). Redfish/IPMI both work. Identity
  cross-check: IPMI FRU chassis `1S7DLZCTO1WWJ7024D71`, Redfish product serial
  `00001S7DJVCTO1WWJ70247ZZ`, and Intel I210 MAC `c4:ef:bb:1b:08:b8`.
- The one-time UEFI HTTP discovery boot succeeded. Installation wrote the
  OS SSD and entered Rebooting at 08:58:48 UTC. Firmware then retried network
  boot rather than starting the installed OS; SOL confirmed HTTP boot/NBP
  timeouts.
- Recovery: selected the installed Red Hat UEFI device path through a
  one-time Redfish override, then restarted **tray-3 only**. It became
  reachable by SSH on the bootstrap RHCOS 9.6 / kernel 5.14 / 4 KiB image.
  At approximately 09:31 UTC, `efibootmgr` verified `BootCurrent: 0000` and
  was used to put the installed Red Hat entry first in BootOrder, plus
  `BootNext: 0000` for the upcoming automatic OS-update reboot.
- At approximately 09:33 UTC, the Machine Config Daemon image pull completed
  and `firstboot-complete-machineconfig` was running. The target is the
  NodePool's RHCOS 10 / NVIDIA 6.12 64 KiB image and existing host tuning.
  Kubelet cannot run on the incomplete bootstrap image yet; do not manually
  install CRI-O or replace kubelet files while the first-boot update runs.
- A read-only readiness monitor watches Agent progress, NodePool health,
  target kernel, hugepages and MCD convergence, and checks original worker
  boot IDs / VMI UIDs remain unchanged. Step 1 is not complete until the
  new worker is Ready and stable. Step 2 (new VM preparation) still requires
  user confirmation.
- Private pre-change backups:
  `/home/nvidia/workspaces/skaufman/task_hcp/maintenance/20261104T081803Z-tray3-resume/`
  and `maintenance/20261104T082709Z-worker-placement/`.
  Onboarding evidence and `progress.log` are under
  `maintenance/20261104T084244Z-tray3-onboarding/`.

## Historical state at suspension (2026-11-03; superseded above)

| Item | State |
|---|---|
| our cluster (my-hosted-cluster) | 2/2 workers Ready (tray-11, tray-12), NodePool healthy |
| jose's cluster | 1/1 worker (tray-8) — tray-3 was detached cleanly (agent deleted, NodePool scaled 2→1, his control plane healthy) |
| tray-3 host | Orphaned: running jose's RHCOS joined to nothing; ping OK; local-disk-first boot order in effect (safe) |
| tray-3 iPXE entrypoint | **Dormant/safe**: reverted to `exit` (backup of the discovery version below) |
| nginx boot artifacts | **Staged and persistent**: `/usr/share/nginx/ipxe/ipxe/my-hosted-cluster/0306e33f/` (vmlinuz, initrd.img, ignition.img, assisted_installer_custom.img, nmstate.img, rootfs.img) — all URLs verified HTTP 206 |
| Our InfraEnv | `clusters/my-hosted-cluster`, id `0306e33f-29af-4ed6-9008-b6f8155c4982` (artifacts extracted from its minimal ISO) |
| Discovery boot chain | Matches the ISO's grub.cfg exactly: kernel cmdline `rw ignition.firstboot ignition.platform.id=metal coreos.live.rootfs_url=<local rootfs>` + 4 initrds |

## Key facts for resuming

- tray-3: host 172.16.2.105, BMC 172.16.2.63 (user `admin`), mgmt MAC
  `c4:ef:bb:1b:08:b8`, iPXE entrypoint
  `/usr/share/nginx/ipxe/ipxe/ubuntu_24_04-c4-ef-bb-1b-08-b8.ipxe`
- Boot server = the bastion itself (172.16.0.1, nginx on :8080, sudo access)
- Tray-3's previously selected OS SSD is 1.92 TB, serial `S666NN0Y713655`,
  stable ID `/dev/disk/by-id/nvme-eui.36363630597136550025384e00000015`
  (previously `nvme2n1`). Revalidated in discovery on 2026-11-04; the Agent's
  installation field is `spec.installation_disk_id`. The earlier notes incorrectly listed serial
  `S666NN0Y713497` as a second tray-3 option; it belongs to tray-8. Do not
  select it or any 3.84 TB data SSD.
- Hub-assisted API needs the hub ingress CA: `/home/nvidia/workspaces/
  skaufman/task_hcp/iso-tray12/hub-ingress-ca.crt` (curl --cacert)
- jose's dir `/home/nvidia/workspaces/skaufman/task_hcp/jose-hosted-cluster/`
  has full reference material (boot logs, disk inventory, BMC history)
- After install completes: restore the `exit` iPXE entrypoint (network-first
  hardware — do not leave a bootable network entrypoint active) and verify
  local-disk-first boot order, exactly as jose's deployment did

## Discovery procedure (historical reference; do not rerun during installation)

1. First verify BMC access and physical identity (tray-3 chassis
   `1S7DLZCTO1WWJ7024D71`), InfraEnv, and boot artifacts. Then re-arm only
   tray-3's discovery entrypoint (this exact staged boot chain successfully
   booted tray-3 on 2026-11-04):

   ```bash
   E=/usr/share/nginx/ipxe/ipxe/ubuntu_24_04-c4-ef-bb-1b-08-b8.ipxe
   sudo tee $E > /dev/null <<'EOF'
   #!ipxe
   set base http://172.16.0.1:8080/ipxe/my-hosted-cluster/0306e33f
   kernel ${base}/vmlinuz rw ignition.firstboot ignition.platform.id=metal coreos.live.rootfs_url=${base}/rootfs.img
   initrd ${base}/initrd.img
   initrd ${base}/ignition.img
   initrd ${base}/assisted_installer_custom.img
   initrd ${base}/nmstate.img
   boot
   EOF
   ```

2. Select tray-3's verified **one-time UEFI HTTP** boot entry and reboot.
   The earlier successful installation required the exact Intel I210
   `Boot0005` UEFI device path via Redfish; do not assume a generic IPMI
   Force-PXE override is sufficient. Preserve local-disk-first persistent
   boot order. Keep credentials in memory or use password prompting, not
   `-P <password>` on the command line.

   Discovery boots into RAM; it is the subsequent approved installation,
   not the boot alone, that overwrites the selected OS disk. BMC credentials
   are not stored in this repo.

3. Watch the agent appear in the hub (`clusters` namespace), verify its
   management MAC, chassis/inventory, ARM64 architecture, and the OS disk
   serial above before approval. Restore the per-MAC entrypoint to `exit`
   once discovery is running, **before approving installation**, so a
   subsequent reboot cannot re-enter discovery. Set the installation disk,
   approve and scale only our NodePool 2→3. Wait for Ready and host tuning,
   verify tray-11/12 VM instance IDs and boot IDs are unchanged, and check
   tray-3's installed local-disk boot order. Stop for confirmation before
   preparing a new VM.

## After onboarding (the actual goal)

CX-8 RDMA between a new VM on tray-3 and gpu-vm-tray-12. Both trays were
observed with eight corresponding CX-8 PF links up at 400G. **Carrier and
matching BDFs do not prove shared fabric/VLAN reachability**; same-rail L2
and verbs connectivity still need testing. The supply/shim approach can be
adapted, but inspect CX-8 VF IDs, IOMMU isolation, NUMA placement, and scripts'
hardcoded BF-3 addresses before applying any CX-8 variant.
