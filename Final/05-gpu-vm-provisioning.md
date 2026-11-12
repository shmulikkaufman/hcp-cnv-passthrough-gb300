# 5 — GPU VM provisioning (final profile)

Provision the two GPU VMs: `gpu-vm-tray-3` (on `compute-tray-3`) and
`gpu-vm-tray-12` (on `compute-tray-12`). Per-VM manifests live in
`configs/04-vm/tray-3/` and `configs/04-vm/tray-12/`; the shared namespace and the
original single-VM manifests are in `configs/04-vm/`. The NIC (CX-8 VF)
hostDevices are added later, in [Chapter 7](07-cx8-rdma-rails.md) — this
chapter builds the GPU-only baseline first, exactly as the stable rollout
sequence requires.

## 5.1 Namespace

```bash
oc apply -f configs/04-vm/00-gpu-vm-namespace.yaml    # gpu-vm-test
```

## 5.2 Root disk: static PVs + DataVolume

Storage here is static local-hostPath (no dynamic provisioning, no
snapshots — plan rollbacks as offline disk copies, see
[`configs/01-hcp/vm-host-tuning.md`](configs/01-hcp/vm-host-tuning.md)).

On the target worker, create the backing directories with CDI-compatible
ownership and SELinux:

```bash
mkdir -p /var/local/kubevirt/<vm>-root /var/local/kubevirt/<vm>-scratch
chown 107:107 /var/local/kubevirt/<vm>-*
chcon -t container_file_t /var/local/kubevirt/<vm>-*
```

Apply the per-VM static PVs and root DataVolume
(`configs/04-vm/tray-3/00-local-pvs.yaml`, `01-root-dv.yaml`; same for tray-12),
then upload the clean image:

```bash
UPLOAD_ROUTE=$(oc get route -n openshift-cnv cdi-uploadproxy -o jsonpath='{.spec.host}')
virtctl image-upload dv <vm>-rhel-root -n gpu-vm-test \
  --size=56Gi --storage-class local-hostpath \
  --image-path=/absolute/path/rhel4nv-26.03-aarch64-kvm.qcow2 \
  --uploadproxy-url=https://$UPLOAD_ROUTE --insecure \
  --access-mode=ReadWriteOnce
```

Operational notes (all hit in practice — details in Appendix A):
`--image-path` must be absolute; the upload route lives in
`openshift-cnv`; with static PVs, size the PV ~15% above the DataVolume
request (CDI adds filesystem overhead); verify the upload route certificate
with the managed cluster's ingress CA rather than blindly trusting
`--insecure`.

## 5.3 Cloud-init

Create the per-VM Secret `<vm>-cloudinit` with a `userdata` key:
hostname, intended **public** SSH keys, `ssh_deletekeys: true` so each
guest gets its own host identity. Never copy another guest's RHSM consumer
identity or cloud-init Secret; never commit credential-bearing user-data.

## 5.4 The VM spec (final stable profile)

Per-VM manifests: `configs/04-vm/tray-3/02-gpu-vm.yaml`,
`configs/04-vm/tray-12/02-gpu-vm.yaml`. The essential spec:

```yaml
spec:
  runStrategy: Always
  template:
    spec:
      architecture: arm64
      nodeSelector:
        kubernetes.io/hostname: compute-tray-3      # per VM
      domain:
        machine:
          type: virt
        cpu:
          cores: 4
          dedicatedCpuPlacement: true
          numa:
            guestMappingPassthrough: {}
        resources:
          requests:
            memory: 16Gi
          limits:
            devices.kubevirt.io/iommufd: "1"
        memory:
          guest: 16Gi
          hugepages:
            pageSize: 2Mi
        devices:
          disks:
          - name: rootdisk
            disk:
              bus: virtio
          - name: cloudinit
            disk:
              bus: virtio
          interfaces:
          - name: default
            masquerade: {}
          gpus:
          - name: gpu0
            deviceName: nvidia.com/31c2
          - name: gpu1
            deviceName: nvidia.com/31c2
      networks:
      - name: default
        pod: {}
      volumes:
      - name: rootdisk
        dataVolume:
          name: <vm>-rhel-root
      - name: cloudinit
        cloudInitNoCloud:
          secretRef:
            name: <vm>-cloudinit
```

Why each non-obvious piece is there:

- `devices.kubevirt.io/iommufd: "1"` — use the kernel iommufd VFIO
  container (libvirt ≥ 12.2); obsoletes the legacy type1 DaemonSet.
- `gpus:` entries — one passed-through GB300 per entry (2 per VM; the
  worker has 4 allocatable). AIE scales the topology automatically:
  `pcihole64` grows to 4 TiB, each GPU gets its own PCIe root port and
  **its own vSMMUv3**, guest enumerates two PCI devices.
- `guestMappingPassthrough` + `dedicatedCpuPlacement` + 2 MiB hugepages —
  required for AIE to generate the vSMMUv3 and GPU ACPI NUMA nodes;
  without the NUMA/hugepage block AIE skips the SMMUv3 entirely.
- **4 vCPUs per VM** (validated 2026-11-05; originally 2). With four RDMA
  rails, one rail per vCPU reaches **full 400G line rate on every rail
  concurrently** — at 2 vCPUs the concurrent per-rail throughput was
  CPU-bound at ~248 Gb/s. The change is a stop/patch/start cycle
  (`domain.cpu.cores: 2 → 4`), keeping all other spec fields.
- No hook sidecars, no `RMForceBarPath` (4.21 legacy, harmful on 4.22).
- The kubelet topology manager runs `restricted`, so GPUs, vCPUs and
  hugepages must co-locate on one NUMA node — a 2-GPU VM fits (one GPU
  pair per NUMA node).

Any QEMU-level change later requires a full VM stop/start; the QEMU
command line is fixed for the life of the process.

## 5.5 SSH access (NodePort services)

```bash
oc apply -f 04-vm/tray-3/03-ssh-service.yaml     # NodePort 30025
oc apply -f 04-vm/tray-12/03-ssh-service.yaml    # NodePort 30024
```

```bash
ssh -i ~/.ssh/hcp_rsa -p 30025 cloud-user@172.16.2.105   # gpu-vm-tray-3
ssh -i ~/.ssh/hcp_rsa -p 30024 cloud-user@172.16.2.114   # gpu-vm-tray-12
```

NodePorts are reachable via any worker IP. `virtctl ssh` and libvirt
`guest-exec` are not viable here (masquerade timeouts, launcher agent
allowlist) — NodePort SSH is the access path. Use a dedicated
`UserKnownHostsFile` per VM (host keys change if a NodePort is reused by a
different VM).

## 5.6 Verification

```bash
oc get vmi -n gpu-vm-test -o wide        # Running / Ready on the right node
# in the guest:
lspci | grep -i nvidia                   # two GB300s (e.g. fd:00.0, ff:00.0)
uname -r                                 # 6.12.0-259.26.el10nv.aarch64
getconf PAGESIZE                         # 65536
```

PCI enumeration alone is **not** GPU acceptance — the driver/CUDA
validation of Chapter 6 is the acceptance gate.

## 5.7 Second VM rollout discipline

The second VM was deployed only after the first was fully validated
(driver + CUDA). Keep that order for new trays: complete host tuning
(Chapter 2), verify the first VM end-to-end, then clone the procedure with
independent storage, independent cloud-init identity and a free NodePort.
Never reuse another guest's registered root disk.
