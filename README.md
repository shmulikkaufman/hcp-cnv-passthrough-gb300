# HCP + OpenShift Virtualization + GB300 GPU Passthrough on ARM64

End-to-end reference for running an NVIDIA GB300 (Grace-Blackwell) GPU inside a
KubeVirt virtual machine on a hosted (HCP / HyperShift) OpenShift cluster.

The setup was built and validated on a GB300 NVL72 system. Last fully
re-validated **2026-11-01**: `nvidia-smi` detects the GPU, the CUDA driver API
works (`cuInit`/`cuCtxCreate`/`cuMemAlloc`), and cuda-samples `deviceQuery` +
`vectorAdd` build and run for `sm_103` inside the VM.

| Component | Value |
|---|---|
| Hub cluster | OpenShift 5.0.0-rc.6, x86_64, node `hub-node-1` (172.16.2.132) |
| Guest (hosted) cluster | OpenShift 4.22.19 HCP, Agent platform, multi-arch payload |
| Guest worker node | `compute-tray-11` (172.16.2.113), RHCOS 10.2 + RHEL4NV `kernel-64k-core-6.12.0-259.26.el10nv` (64 KiB pages), arm64, 4x GB300 |
| Virtualization | OpenShift Virtualization (CNV) 4.22.12 **+ AIE** (webhook, iommufd device plugin, `virt-launcher-4-nv-rhel10`) |
| GPU operator | NVIDIA GPU Operator 26.7.2, vm-passthrough (sandbox) mode |
| Guest VM | RHEL 10.2 ARM64 (`rhel4nv-26.03-aarch64-kvm.qcow2`), kernel 6.12.0-259.26.el10nv, 64 KiB pages |
| Guest driver | NVIDIA open kernel modules 615.71.10 + CUDA toolkit 13.4 |

Final result — `nvidia-smi` inside the VM (2026-11-01):

```
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 615.71.10              KMD Version: 615.71.10     CUDA UMD Version: 13.4     |
+-----------------------------------------+----------------------------------------+------+
|   0  NVIDIA GB300                   On  |       00000000:FF:00.0 Off |                    0 |
| N/A   31C    P0            246W / 1400W |       0MiB / 284208MiB |      0%      Default |
+-----------------------------------------------------------------------------------------+
```

> **CNV 4.22 migration warning:** two workarounds from the CNV 4.21 stack —
> the MMIO-window hook sidecar (Phase 4.3) and the guest
> `RMForceBarPath=1` modprobe option (Phase 5.2) — are **actively harmful**
> on CNV 4.22 + AIE and were the root cause of weeks of
> `kbusVerifyBar2_GH100: MMUTest BAR0 window … returned garbage 0x0`
> failures. Both sections below are kept for historical reference only and
> marked ⚠️ LEGACY. Do **not** apply them on 4.22+.

## Architecture

```text
x86_64 hub cluster (OpenShift 5.0)
  └─ HostedControlPlane pods (my-hosted-cluster, namespace: clusters)
       └─ ARM64 guest worker node (compute-tray-11, booted via Agent/discovery ISO,
            RHCOS 10.2 + RHEL4NV kernel-64k-core 6.12.0-259.26.el10nv)
            ├─ OpenShift Virtualization (CNV 4.22 / KubeVirt + AIE)
            │    └─ VM gpu-vm (RHEL 10.2 arm64, 64 KiB pages)
            │         └─ GB300 passed through via VFIO/iommufd
            │              ├─ AIE-generated: vSMMUv3 (accel/ATS/PASID),
            │              │  iommufd FD, GPU NUMA nodes 1-8, pcihole64 2 TiB
            │              └─ NVIDIA driver 615.71.10 (coherent path — no regkeys)
            └─ NVIDIA GPU operator (vm-passthrough mode)
                 ├─ vfio-manager: binds GB300s to nvgrace_gpu_vfio_pci
                 └─ sandbox-device-plugin: advertises nvidia.com/31c2
```

Division of labor for GPU passthrough:

* **GPU operator (node side)** — binds the GPU to `nvgrace_gpu_vfio_pci` (the
  Grace/Blackwell VFIO variant driver) and advertises it as
  the extended resource `nvidia.com/31c2` so KubeVirt can schedule a VM with it.
  It deliberately installs **no node driver** in vm-passthrough mode.
* **Everything inside the VM** — kernel driver, userspace tools — is manual
  (see [Phase 5](#phase-5--in-guest-nvidia-driver)), because a plain RHEL VM is
  not a Kubernetes node and the operator cannot reach inside it.

## Repository layout

```text
01-hcp/                          Hosted control plane (hub + guest cluster)
  create-guest-cluster.sh        hcp CLI invocation
  hostedcluster.yaml             resulting HostedCluster (reference)
  nodepool.yaml                  resulting NodePool (reference)
  infraenv.yaml                  discovery ISO environment (reference)
02-cnv/                          OpenShift Virtualization operator + HyperConverged + AIE
03-gpu-operator/                 GPU operator, vm-passthrough policy, CNV device whitelist
04-vm/                           The GPU VM: DataVolume, VM, SSH service
05-guest-driver/                 In-guest NVIDIA driver install
06-cuda-test/                    CUDA validation: driver-API probe + cuda-samples build/run
scripts/                         Environment helpers
```

All manifests are also inlined in the phase sections below.

---

# Phase 0 — Prerequisites

* Hub OpenShift cluster (x86_64 here) with:
  * `multicluster-engine` installed (standalone MCE is enough; ACM is optional)
  * a default `StorageClass` for the hosted etcd (`lvms-vg1` here — from the LVMS
    operator; any RWO block storage works)
  * a LoadBalancer story for on-prem: this setup exposes all hosted-cluster
    services as NodePort on the hub address 172.16.2.132, so MetalLB is not
    required
* `hcp` CLI (HyperShift) on your workstation, matching the hub version
* Pull secret (`pull-secret.json`), an SSH keypair for node access
  (`~/.ssh/hcp_rsa`), and the ARM64 worker hardware (GB300 tray) able to
  PXE/ISO boot

Environment:

```bash
export KUBECONFIG=~/workspaces/skaufman/task_hcp/kubeconfig_hub
```

Hub readiness gaps to be aware of (`scripts/check_hub_readiness.sh` summarizes
them): MCE sufficient (ACM optional), CNV only needed if workers are VMs,
MetalLB mandatory for on-prem LB (avoided here by NodePort), default
StorageClass mandatory for hosted etcd.

---

# Phase 1 — HCP: hub + hosted (guest) cluster

## 1.1 Create the hosted cluster

```bash
# scripts/setenv.sh equivalent
export KUBECONFIG=$PWD/kubeconfig_hub

./hcp create cluster agent \
  --name my-hosted-cluster \
  --namespace clusters \
  --agent-namespace clusters \
  --arch arm64 \
  --release-image quay.io/openshift-release-dev/ocp-release:4.22.0-multi \
  --pull-secret "$PWD/pull-secret.json" \
  --ssh-key /home/nvidia/.ssh/hcp_rsa.pub \
  --base-domain 172-16-2-132.sslip.io \
  --api-server-address 172.16.2.132 \
  --network-type OVNKubernetes \
  --cluster-cidr 10.132.0.0/14 \
  --service-cidr 172.31.0.0/16 \
  --control-plane-availability-policy SingleReplica \
  --infra-availability-policy SingleReplica \
  --etcd-storage-class lvms-vg1 \
  --etcd-storage-size 8Gi \
  --node-pool-replicas 1 \
  --node-upgrade-type InPlace
```

Two ARM64/multi-arch gotchas baked into that command:

* **Release image must be the multi-arch payload**
  (`ocp-release:4.22.0-multi`). Control-plane pods are generated by the x86_64
  hub, so a plain `4.22.0-aarch64` image fails with
  `cluster-config-api: cannot execute binary file: Exec format error`.
* **Base domain must be hyphenated**: `172-16-2-132.sslip.io`. The dotted form
  `172.16.2.132.sslip.io` is rejected as an invalid DNS label.

## 1.2 Resulting HostedCluster (reference)

`01-hcp/hostedcluster.yaml`:

```yaml
apiVersion: hypershift.openshift.io/v1beta1
kind: HostedCluster
metadata:
  name: my-hosted-cluster
  namespace: clusters
spec:
  autoscaling:
    scaling: ScaleUpAndScaleDown
  controllerAvailabilityPolicy: SingleReplica
  dns:
    baseDomain: 172-16-2-132.sslip.io
  etcd:
    managed:
      storage:
        persistentVolume:
          size: 8Gi
          storageClassName: lvms-vg1
        type: PersistentVolume
    managementType: Managed
  fips: false
  infrastructureAvailabilityPolicy: SingleReplica
  networking:
    allocateNodeCIDRs: Disabled
    clusterNetwork:
    - cidr: 10.132.0.0/14
    networkType: OVNKubernetes
    serviceNetwork:
    - cidr: 172.31.0.0/16
  olmCatalogPlacement: management
  platform:
    agent:
      agentNamespace: clusters
    type: Agent
  pullSecret:
    name: my-hosted-cluster-pull-secret
  release:
    image: quay.io/openshift-release-dev/ocp-release:4.22.0-multi
  secretEncryption:
    aescbc:
      activeKey:
        name: my-hosted-cluster-etcd-encryption-key
    type: aescbc
  services:
  - service: APIServer
    servicePublishingStrategy:
      nodePort:
        address: 172.16.2.132
      type: NodePort
  - service: Ignition
    servicePublishingStrategy:
      nodePort:
        address: 172.16.2.132
      type: NodePort
  - service: Konnectivity
    servicePublishingStrategy:
      nodePort:
        address: 172.16.2.132
      type: NodePort
  - service: OAuthServer
    servicePublishingStrategy:
      nodePort:
        address: 172.16.2.132
      type: NodePort
  - service: OIDC
    servicePublishingStrategy:
      nodePort:
        address: 172.16.2.132
      type: NodePort
  sshKey:
    name: my-hosted-cluster-ssh-key
```

## 1.3 Resulting NodePool (reference)

`01-hcp/nodepool.yaml`:

```yaml
apiVersion: hypershift.openshift.io/v1beta1
kind: NodePool
metadata:
  name: my-hosted-cluster
  namespace: clusters
spec:
  arch: arm64
  clusterName: my-hosted-cluster
  management:
    autoRepair: false
    replace:
      rollingUpdate:
        maxSurge: 1
        maxUnavailable: 0
      strategy: RollingUpdate
    upgradeType: InPlace
  platform:
    agent: {}
    type: Agent
  release:
    image: quay.io/openshift-release-dev/ocp-release:4.22.0-multi
  replicas: 1
```

## 1.4 InfraEnv + discovery ISO

`01-hcp/infraenv.yaml` (created by the CLI):

```yaml
apiVersion: agent-install.openshift.io/v1beta1
kind: InfraEnv
metadata:
  name: my-hosted-cluster
  namespace: clusters
spec:
  cpuArchitecture: arm64
  ipxeScriptType: DiscoveryImageAlways
  nmStateConfigLabelSelector: {}
  pullSecretRef:
    name: pull-secret
  sshAuthorizedKey: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA<replace-with-your-public-key> user@example.com
```

Download the discovery ISO and boot the GB300 tray from it:

```bash
oc get infraenv my-hosted-cluster -n clusters -o jsonpath='{.status.isoDownloadURL}'
# -> minimal.iso; boot the worker from it (virtual media / USB). The host registers
#    as an Agent in namespace `clusters`; HCP binds it to the NodePool and ignites it.
```

## 1.5 Get the guest kubeconfig

```bash
oc get secret my-hosted-cluster-admin-kubeconfig -n clusters \
  -o jsonpath='{.data.kubeconfig}' | base64 -d > guest-kubeconfig
export KUBECONFIG=$PWD/guest-kubeconfig
oc get nodes
# NAME             STATUS   ROLES   AGE   VERSION
# compute-tray-11  Ready    worker  ...   v1.36.3
```

---

# Phase 2 — Deploy OpenShift Virtualization (CNV)

Applied with the **guest** kubeconfig. `02-cnv/`:

```yaml
# 00-cnv-namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-cnv
```

```yaml
# 01-cnv-operatorgroup.yaml
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: kubevirt-hyperconverged-group
  namespace: openshift-cnv
spec:
  targetNamespaces:
  - openshift-cnv
```

```yaml
# 02-cnv-subscription.yaml
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: kubevirt-hyperconverged
  namespace: openshift-cnv
spec:
  channel: stable
  installPlanApproval: Automatic
  name: kubevirt-hyperconverged
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  startingCSV: kubevirt-hyperconverged-operator.v4.22.12
```

Wait for the CSV to report Succeeded, then create the HyperConverged CR:

```yaml
# 03-hyperconverged.yaml
apiVersion: hco.kubevirt.io/v1beta1
kind: HyperConverged
metadata:
  name: kubevirt-hyperconverged
  namespace: openshift-cnv
spec: {}
```

```bash
oc apply -f 02-cnv/
oc get csv -n openshift-cnv           # Succeeded
oc get pods -n openshift-cnv | head   # virt-operator, virt-handler, cdi, etc.
```

## 2.1 Enable AIE (mandatory for GB300 passthrough on CNV 4.22)

AIE (Accelerated Infrastructure Enablement) provides the NVIDIA-tuned
virt-launcher, the IOMMUFD device plugin and the SMMUv3/NUMA generation that
GB300 passthrough depends on. Without it the standard launcher cannot lay out
the GPU correctly.

```bash
oc annotate hyperconverged kubevirt-hyperconverged -n openshift-cnv \
  hco.kubevirt.io/deployAIE=true
```

Verify the AIE operands:

```bash
oc get deployment kubevirt-aie-webhook -n openshift-cnv
oc get daemonset iommufd-device-plugin -n openshift-cnv
```

Configure the AIE launcher image selection — extract the NV launcher from the
installed CSV and wire it into the webhook's ConfigMap
(`02-cnv/04-aie-launcher-config.yaml`):

```bash
CSV=$(oc get csv -n openshift-cnv -o name | grep kubevirt-hyperconverged-operator | sed 's|.*/||')
LAUNCHER_NV_IMAGE=$(oc get csv $CSV -n openshift-cnv -o json \
  | jq -r '.spec.relatedImages[] | select(.name | test("virt-launcher-4-nv")) | .image')
```

```yaml
# 02-cnv/04-aie-launcher-config.yaml (substitute ${LAUNCHER_NV_IMAGE})
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubevirt-aie-launcher-config
  namespace: openshift-cnv
data:
  config.yaml: |
    rules:
    - name: "nvidia-gpu-launcher"
      image: "${LAUNCHER_NV_IMAGE}"
      selector:
        deviceNames:
        - "nvidia.com/31c2"
    - name: "labeled-vms"
      image: "${LAUNCHER_NV_IMAGE}"
      selector:
        vmLabels:
          matchLabels:
            aie.kubevirt.io/launcher: "true"
```

Rules are evaluated in order; the first match wins. A VM requesting
`nvidia.com/31c2` is mutated to the NV launcher automatically — no VM label
required (the label rule is a fallback for VMs without GPU resources).

---

# Phase 3 — GPU operator in vm-passthrough mode + device whitelist

## 3.1 Install the GPU operator

```yaml
# 03-gpu-operator/00-gpu-operator-namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: nvidia-gpu-operator
```

```yaml
# 03-gpu-operator/01-gpu-operator-operatorgroup.yaml
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nvidia-gpu-operator-group
  namespace: nvidia-gpu-operator
spec:
  targetNamespaces:
  - nvidia-gpu-operator
```

```yaml
# 03-gpu-operator/02-gpu-operator-subscription.yaml
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: gpu-operator-certified
  namespace: nvidia-gpu-operator
spec:
  channel: stable
  installPlanApproval: Automatic
  name: gpu-operator-certified
  source: certified-operators
  sourceNamespace: openshift-marketplace
  startingCSV: gpu-operator-certified.v26.7.2
```

## 3.2 ClusterPolicy — KubeVirt vm-passthrough mode

This is the key configuration: the operator manages the node side only
(vfio binding + resource advertisement), no node driver.

```yaml
# 03-gpu-operator/03-gpu-cluster-policy.yaml
apiVersion: nvidia.com/v1
kind: ClusterPolicy
metadata:
  name: gpu-cluster-policy
spec:
  operator:
    use_ocp_driver_toolkit: true
  sandboxWorkloads:
    enabled: true
    defaultWorkload: vm-passthrough
    mode: kubevirt
  sandboxDevicePlugin:
    enabled: true
  vfioManager:
    enabled: true
  driver:
    enabled: true
    useNvidiaDriverCRD: false
    kernelModuleType: auto
  dcgm:
    enabled: true
  dcgmExporter:
    enabled: true
    serviceMonitor:
      enabled: true
  daemonsets:
    updateStrategy: RollingUpdate
    rollingUpdate:
      maxUnavailable: "1"
  devicePlugin:
    enabled: true
  gfd:
    enabled: true
  nodeStatusExporter:
    enabled: true
  toolkit:
    enabled: true
  validator:
    plugin:
      env: []
```

In this mode expect these pods (and **no** `nvidia-driver-daemonset`):

```text
nvidia-vfio-manager-...
nvidia-sandbox-device-plugin-daemonset-...
nvidia-sandbox-validator-...
gpu-operator-...
```

## 3.3 Whitelist the GPU as a CNV permitted host device

Without this, the VM cannot consume `nvidia.com/31c2`:

```yaml
# 03-gpu-operator/04-cnv-permitted-host-devices.yaml
apiVersion: hco.kubevirt.io/v1beta1
kind: HyperConverged
metadata:
  name: kubevirt-hyperconverged
  namespace: openshift-cnv
spec:
  permittedHostDevices:
    pciHostDevices:
    - pciDeviceSelector: 10de:31c2
      resourceName: nvidia.com/31c2
      externalResourceProvider: true
```

`externalResourceProvider: true` tells KubeVirt the resource is advertised by
the GPU operator's sandbox device plugin rather than by KubeVirt itself.

Verify the resource appears on the node:

```bash
oc get node compute-tray-11 -o json | jq '.status.allocatable["nvidia.com/31c2"]'
# "4"
```

## 3.4 (Legacy) VFIO type1 fix — only for older virt-launcher

`03-gpu-operator/05-gpu-vfio-type1-fix.yaml` + `06-gpu-vfio-fix-rbac.yaml`
deploy a privileged DaemonSet that:

1. loads `vfio_iommu_type1` on the node (RHCOS 9.8 ships it but does not
   auto-load it; without it VFIO has "No available IOMMU models"), and
2. keeps vfio-bound NVIDIA GPUs out of runtime suspend (a suspended GPU returns
   garbage PCI config reads: `Unknown PCI header type 127`).

**Not needed anymore** with virt-launcher builds that support iommufd
(libvirt >= 12.2) — the VM below requests `devices.kubevirt.io/iommufd: "1"`.
Kept in the repo for older stacks. Full manifests are in
`03-gpu-operator/05-gpu-vfio-type1-fix.yaml` and `06-gpu-vfio-fix-rbac.yaml`.

---

# Phase 4 — Create the GPU VM

## 4.1 Namespace

```yaml
# 04-vm/00-gpu-vm-namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: gpu-vm-test
```

## 4.2 Root disk (DataVolume + image upload)

The guest OS is a RHEL 10 ARM64 KVM image (`rhel4nv-26.03-aarch64-kvm.qcow2`,
kernel `6.12.0-259.26.el10nv`). Create the DataVolume and upload the qcow2:

```yaml
# 04-vm/01-gpu-vm-root-dv.yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: gpu-vm-rhel-root
  namespace: gpu-vm-test
spec:
  contentType: kubevirt
  source:
    upload: {}
  storage:
    accessModes:
    - ReadWriteOnce
    resources:
      requests:
        storage: 56Gi
    storageClassName: local-hostpath
```

```bash
UPLOAD_ROUTE=$(oc get route -n openshift-cnv cdi-uploadproxy -o jsonpath='{.spec.host}')
virtctl image-upload dv gpu-vm-rhel-root -n gpu-vm-test \
  --size=56Gi --storage-class=local-hostpath \
  --image-path="$PWD/rhel4nv-26.03-aarch64-kvm.qcow2" \
  --uploadproxy-url=https://$UPLOAD_ROUTE --insecure \
  --access-mode=ReadWriteOnce
```

Upload gotchas (all hit in practice — see the troubleshooting table):
`--image-path` must be an **absolute path** (`virtctl` does not expand `~` or
relative paths reliably), the `cdi-uploadproxy` route lives in
`openshift-cnv` (not `cdi`), and with static local PVs the backing host
directories need `chown 107:107` + `chcon -t container_file_t`, with the PV
sized ~15% above the DataVolume request (CDI adds filesystem overhead).

## 4.3 ⚠️ LEGACY — MMIO window hook sidecar (CNV 4.21 only; HARMFUL on 4.22)

> **Do not deploy this on CNV 4.22 + AIE.** AIE sizes the guest PCI window
> natively (`pcihole64` = 2 TiB, visible in the live domain XML) and lays out
> the GB300's BARs and coherent-memory description itself. The hook's
> `-machine highmem-mmio-size=16T` override arrives *after* AIE's calculation
> and corrupts that layout: the guest driver then fails with
> `kbusVerifyBar2_GH100: MMUTest BAR0 window offset 0x70e000 returned garbage 0x0`
> and `RmInitAdapter failed (0x24:0x72:1298)`. This was one of the two root
> causes of the 4.22 regression (the other: Phase 5.2 `RMForceBarPath`).

Historical context (CNV 4.21, QEMU without AIE): the GB300 exposes ~520 GiB
of PCI BARs (BAR2 = 512 GiB via Resizable BAR) while the QEMU `virt` machine's
default 64-bit MMIO window is only 512 GiB, so the guest kernel could not
assign the BARs and the NVIDIA driver failed with `NVRM: BAR0 is 0M @ 0x0`.
The hook below was the workaround. Kept for reference only —
`04-vm/gpu-mmio-hook.sh`:

```bash
oc -n gpu-vm-test create configmap gpu-mmio-hook \
  --from-file=onDefineDomain.sh=04-vm/gpu-mmio-hook.sh
```

```sh
#!/bin/sh
# KubeVirt sidecar hook (onDefineDomain): enlarge the guest 64-bit PCI MMIO
# window so the GB300's 512 GiB BAR fits.
#
# QEMU >= 9.x/10.x gotcha: -global gpex-pcihost.x-above-4g-mmio-size=16T is
# ACCEPTED but has no effect — the pcie-mmio-high alias is built from the
# machine memmap. The correct knob is the MACHINE property
#   -machine highmem-mmio-size=16T
# which merges with libvirt's own -machine virt-rhel10.2.0 when injected via
# qemu:commandline. Resulting window: 16-32 TiB (host-passthrough Grace CPU
# has 48-bit PA; 1 TiB would also suffice for ~520 GiB of BARs).
set -eu
domain=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain)
      domain="$2"
      shift 2
      ;;
    --vmi)
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
[ -n "$domain" ] || { echo "missing --domain argument" >&2; exit 1; }
if ! printf '%s\n' "$domain" | grep -q 'xmlns:qemu='; then
  domain=$(printf '%s\n' "$domain" | sed 's|<domain |<domain xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0" |')
fi
if ! printf '%s\n' "$domain" | grep -q '<qemu:commandline>'; then
  domain=$(printf '%s\n' "$domain" | sed 's|</domain>|<qemu:commandline><qemu:arg value="-machine"/><qemu:arg value="highmem-mmio-size=16T"/></qemu:commandline></domain>|')
fi
printf '%s\n' "$domain"
```

## 4.4 The VM

`04-vm/02-gpu-vm.yaml` — the exact spec that works on CNV 4.22 + AIE
(validated 2026-11-01):

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: gpu-vm
  namespace: gpu-vm-test
spec:
  runStrategy: Always
  template:
    metadata:
      labels:
        kubevirt.io/domain: gpu-vm
        kubevirt.io/vm: gpu-vm
    spec:
      architecture: arm64
      nodeSelector:
        kubernetes.io/hostname: compute-tray-11
      domain:
        machine:
          type: virt
        cpu:
          cores: 2
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
          - name: gpu1
            deviceName: nvidia.com/31c2
      networks:
      - name: default
        pod: {}
      volumes:
      - name: rootdisk
        dataVolume:
          name: gpu-vm-rhel-root
      - name: cloudinit
        cloudInitNoCloud:
          userData: |
            #cloud-config
            hostname: gpu-vm
            ssh_authorized_keys:
            - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA<replace-with-your-public-key> user@example.com
```

Notes:

* `devices.kubevirt.io/iommufd: "1"` makes virt-launcher use the kernel iommufd
  VFIO container (requires a libvirt >= 12.2 build) — this is what obsoletes
  the Phase 3.4 type1 workaround.
* `gpus: deviceName: nvidia.com/31c2` is the passthrough request; the AIE
  webhook (rule from 2.1) swaps the launcher to `virt-launcher-4-nv-rhel10`.
* `cpu.numa.guestMappingPassthrough` + `dedicatedCpuPlacement` +
  `memory.hugepages` are required for AIE to generate the vSMMUv3
  (`accel=on ats=on ssidSize=20 oas=48`), the GPU ACPI NUMA nodes (1-8) and
  hugepage-backed memfd memory. Without the NUMA/hugepage block AIE skips
  the SMMUv3 device entirely.
* **No hook sidecar annotation** — see 4.3. On 4.22 AIE computes the PCI
  window (`pcihole64` = 2 TiB) itself.
* Any qemu-level change requires a full VM stop/start — the QEMU command
  line is fixed for the life of the process.

## 4.5 SSH access

```yaml
# 04-vm/03-gpu-vm-ssh-svc.yaml
apiVersion: v1
kind: Service
metadata:
  name: gpu-vm-ssh
  namespace: gpu-vm-test
spec:
  type: NodePort
  selector:
    kubevirt.io/domain: gpu-vm
  ports:
  - name: ssh
    port: 22
    targetPort: 22
    nodePort: 30022
```

```bash
ssh -p 30022 cloud-user@172.16.2.113
```

(`virtctl ssh` through the masquerade pod network also exists but timed out in
this environment; the NodePort service is the reliable path. The QEMU
guest-agent `guest-exec` is disabled by libvirt's allowlist in virt-launcher,
so there is no `virsh`-based exec either — SSH is the way in.)

Verify the passthrough from the cluster side:

```bash
oc get vmi -n gpu-vm-test gpu-vm -o wide     # Running, Ready
oc exec -n gpu-vm-test virt-launcher-... -c compute -- \
  virsh dumpxml gpu-vm-test_gpu-vm | grep -A3 hostdev
```

---

# Phase 5 — In-guest NVIDIA driver

Inside the VM (RHEL 10.2 ARM64, 64 KiB pages). On the clean
`rhel4nv-26.03-aarch64-kvm.qcow2` image, register and attach the repos first
(a stock RHEL image also needs the CUDA repo in step 2):

```bash
# 0. Register (RHEL for NVIDIA entitlement; kernel/driver channel comes from here)
sudo subscription-manager register --org=<org> --activationkey=<key>
sudo subscription-manager repos --enable=rhel-10-for-aarch64-nvidia-rpms

# 1. CRB + EPEL (dkms, egl-* build dependencies of the driver packages)
sudo subscription-manager repos --enable=codeready-builder-for-rhel-10-aarch64-rpms
sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm

# 2. Public NVIDIA CUDA repo (nvidia-driver*, nvidia-driver-cuda, kmod)
sudo dnf config-manager --add-repo \
  https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/cuda-rhel10.repo
```

Then install, pinning kmod, userspace **and kernel-devel to the running
kernel** (otherwise dnf pulls a newer `kernel-core` and DKMS builds for the
wrong kernel):

```bash
DRIVER_VERSION=615.71.10
KERNEL_VERSION=$(uname -r | sed 's/+64k//')   # e.g. 6.12.0-259.26.el10nv

sudo dnf install -y \
  kernel-64k-devel-${KERNEL_VERSION} \
  kernel-devel-matched-${KERNEL_VERSION} \
  kmod-nvidia-open-dkms-${DRIVER_VERSION} \
  nvidia-driver-${DRIVER_VERSION} \
  nvidia-modprobe-${DRIVER_VERSION}

# nvidia-smi ships here (NOT in nvidia-driver or the kmod package)
sudo dnf install -y nvidia-driver-cuda-${DRIVER_VERSION}

sudo reboot    # let DKMS module load on a fresh boot
```

Version pinning matters: the kmod and userspace versions must match exactly.
Verify after reboot: `sudo dkms status` should show
`nvidia/615.71.10, 6.12.0-259.26.el10nv.aarch64: installed`.

## 5.1 ⚠️ LEGACY — guest PCI MMIO window (CNV 4.21 only; see Phase 4.3)

On CNV 4.21 (no AIE), if BARs cannot be assigned you will see, inside the VM:

```text
pci 0000:0a:00.0: BAR 2 [mem size 0x8000000000 64bit pref]: can't assign; no space
NVRM: This PCI I/O region assigned to your NVIDIA device is invalid:
NVRM: BAR0 is 0M @ 0x0 (PCI:0000:0a:00.0)
```

That was the symptom of the missing `highmem-mmio-size` machine property —
fixed by the hook sidecar in Phase 4.3. **On CNV 4.22 + AIE do not apply that
hook**: AIE sizes the window natively and the hook breaks GPU init (see 4.3).
On 4.22, verify in the guest after boot:

```bash
sudo dmesg | grep "ff:00.0.*assigned"      # all three BARs assigned
sudo lspci -vvv -s ff:00.0 | grep Region   # non-empty Region lines
```

## 5.2 ⚠️ LEGACY — RMForceBarPath (CNV 4.21 only; HARMFUL on 4.22)

On CNV 4.21 the driver failed even with BARs assigned, because the RM expects
the GPU's coherent (NVLink-C2C) memory window to be described by platform
firmware (ACPI DSD) — which a 4.21 VM does not have:

```text
NVRM: GPU0 kmemsysSetupCoherentCpuLink_IMPL: Failed to get coherent GPU memory base address
NVRM: GPU0 RmInitNvDevice: *** Cannot initialize the device
```

The escape hatch was the `RMForceBarPath` registry key ("disable coherent
path CPU->Nvlink/C2C->FB and force BAR path"):

```bash
# 05-guest-driver/nvidia-forcebarpath.conf -> /etc/modprobe.d/   (4.21 ONLY)
echo 'options nvidia NVreg_RegistryDwords="RMForceBarPath=1"' | \
  sudo tee /etc/modprobe.d/nvidia-forcebarpath.conf
sudo reboot
```

**Do not use this on CNV 4.22 + AIE.** The 4.22 NV launcher provides the
coherent-memory description, and forcing the BAR path instead sends the
driver into the failing `kbusVerifyBar2_GH100` BAR0-window test:

```text
NVRM: GPU0 kbusVerifyBar2_GH100: MMUTest BAR0 window offset 0x70e000 returned garbage 0x0
NVRM: GPU0 RmInitNvDevice: *** Cannot initialize the device
NVRM: GPU 0000:ff:00.0: RmInitAdapter failed! (0x24:0x72:1298)
```

This was the second root cause of the 4.22 regression. If the file exists,
remove it and reboot:

```bash
sudo rm -f /etc/modprobe.d/nvidia-forcebarpath.conf && sudo reboot
```

**Warning (applies to any failed probe):** do not attempt `modprobe -r
nvidia` to reload after a failed probe — the unload hangs. Reboot the guest.

On 4.22 a successful boot logs simply:

```text
NVRM: loading NVIDIA UNIX Open Kernel Module for aarch64  615.71.10  Release Build
```

with **no** `kmemsysSetupCoherentCpuLink` and **no** `kbusVerifyBar2` errors.

## 5.3 Verify

```bash
nvidia-smi
lspci -nnk -s ff:00.0 | grep "Kernel driver in use"   # nvidia
```

---

# Phase 6 — CUDA validation inside the VM

Tooling lives in `06-cuda-test/` (see its README). All examples below are the
**actual commands and outputs from the validated 2026-11-01 run** on
`gpu-vm-rhel-validation` (RHEL 10.2, driver 615.71.10, CUDA 13.4, GB300
`sm_103`), run over the NodePort SSH from the bastion.

## 6.1 Driver-API probe (no toolkit needed)

```bash
# copy 06-cuda-test/cuda-driver-probe.py into the VM (scp -P 30023 ...) or
# paste it into a file there, then:
python3 cuda-driver-probe.py
```

Actual output:

```text
cuInit: 0 CUDA_SUCCESS
cuDeviceGetCount: 0 count: 1
Device 0: NVIDIA GB300 total mem: 283301 MiB
cuCtxCreate: 0
cuMemAlloc(256MiB): 0 ptr: 0xfffb80000000
cuMemFree: 0
```

Proves driver↔GPU communication, device enumeration, context creation and
device-memory alloc/free. Before the 4.22 fix the identical probe returned
`cuInit: 999 CUDA_ERROR_UNKNOWN` — it is the fastest regression check.

## 6.2 Build the samples

```bash
sudo dnf install -y cuda-toolkit-13-4 git cmake
git clone https://github.com/NVIDIA/cuda-samples && cd cuda-samples
export PATH=/usr/local/cuda/bin:$PATH
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_ARCHITECTURES=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
cmake --build build --target deviceQuery vectorAdd -j$(nproc)
```

The GB300 reports compute capability **10.3** → samples build for `sm_103`.
Headless VMs skip the GL/GLUT/Vulkan/MPI/NvSCI samples at configure time
(expected), and the `cdp*` samples skip because they only list `87;110` as
supported architectures.

## 6.3 Quick tests

```bash
./build/cpp/1_Utilities/deviceQuery/deviceQuery
./build/cpp/0_Introduction/vectorAdd/vectorAdd
```

Actual `deviceQuery` output (abridged):

```text
CUDA Device Query (Runtime API) version (CUDART static linking)
Detected 1 CUDA Capable device(s)

Device 0: "NVIDIA GB300"
  CUDA Driver Version / Runtime Version          13.4 / 13.4
  CUDA Capability Major/Minor version number:    10.3
  Total amount of global memory:                 283301 MBytes (...)
  GPU Max Clock rate:                            ...
  Compute Mode:
     < Default (multiple host threads can use ::cudaSetDevice() with device simultaneously) >

deviceQuery, CUDA Driver = CUDART, CUDA Driver Version = 13.4, CUDA Runtime Version = 13.4, NumDevs = 1
Result = PASS
```

Actual `vectorAdd` output (abridged):

```text
[Vector addition of 50000 elements]
Copy input data from the host memory to the CUDA device
CUDA kernel launch with 196 blocks of 256 threads
Copy output data from the CUDA device to the host memory
Test PASSED
Test comparison PASSED
Done
```

## 6.4 Full sample suite

```bash
cmake --build build -j$(nproc)     # build everything
cmake --install build              # populate build/bin/aarch64/linux/release
python3 run_tests.py --dir ./build/bin/aarch64/linux/release --output res --parallel 2
```

Actual result (2026-11-01):

```text
Test Summary:
Ran 118 test runs for 118 executables.
Waived runs (2) — hardware/requirements not met (exit 2), not counted as failure:
  streamOrderedAllocationP2P : Waived (code 2)
  conjugateGradientMultiDeviceCG : Waived (code 2)
Failed runs (1):
  ptxgen : Failed (code 16)
```

So: **115/118 passed, 2 waived, 1 failed**. Interpretation:

- The two waived samples self-waive on a single-GPU VM (they require
  P2P/multi-GPU) — by design, not defects.
- `ptxgen` (exit 16) fails **identically in NVIDIA QE's own working GB300
  run on this same stack** (internal NVIDIA-1408, Oct 2026) — a libNVVM
  sample issue, not a passthrough defect.
- 118 executables run here vs 163 in QE's run because this headless VM skips
  the GL/GLUT/Vulkan/MPI samples at build time; QE's image built them and
  they failed at runtime (26 failures there vs 1 here).

Notable coverage among the 115 passing: all tensor-core GEMMs
(`bf16/fp16/tf32/dmma/immaTensorCoreGemm`), CUDA Graphs
(`simpleCudaGraphs_*`, `jacobiCudaGraphs`, `graphConditionalNodes`),
cooperative groups, Thrust/CUB, unified memory (`uvmlite`,
`UnifiedMemoryStreams`, `UnifiedMemoryPerf`), IPC (`memMapIPCDrv`), JIT/LTO
(`ptxjit`, `jitLto`), multi-GPU enumeration (`simpleMultiGPU`,
`MonteCarloMultiGPU`) and the CUDA Tile samples (`tile*`).

## 6.5 Multi-GPU VM (2x GB300 validated)

Exposing more than one GPU is a one-line-per-GPU spec change — each entry in
`devices.gpus` becomes one passed-through device:

```yaml
        devices:
          gpus:
          - name: gpu0
            deviceName: nvidia.com/31c2
          - name: gpu1
            deviceName: nvidia.com/31c2
```

AIE scales the generated topology automatically (verified in the live domain
XML with 2 GPUs): `pcihole64` grows to 4 TiB, each GPU gets its own PCIe
expander bus + root port + **its own vSMMUv3**, and the guest enumerates two
PCI devices (`fd:00.0`, `ff:00.0`). Actual guest output:

```text
$ nvidia-smi -L
GPU 0: NVIDIA GB300 (UUID: GPU-81e2b4af-...)
GPU 1: NVIDIA GB300 (UUID: GPU-9a5e4a70-...)
```

The samples that self-waive on a single-GPU VM then actually run — and pass
(actual results, 2026-11-01):

```text
simpleMultiGPU                : "Computing with 2 GPUs..."  — PASSED
streamOrderedAllocationP2P    : "Device=0 CAN Access Peer Device=1" — PASSED
conjugateGradientMultiDeviceCG: "Running on GPUs = 2 ... PASSED"
```

Notably **peer-to-peer access between the two passed-through GPUs works**
(`cudaDeviceCanAccessPeer` returns true over the emulated PCIe path) — this
goes beyond the retained QE evidence, whose 2-GPU run output was not saved.

Caveats: the worker's 4 GPUs are split 2+2 across two NUMA nodes and the
kubelet topology manager runs `restricted`, so with
`dedicatedCpuPlacement`/`guestMappingPassthrough` the scheduler must co-locate
GPUs, CPUs and hugepages; a 2-GPU VM fits (one GPU pair per NUMA node). The
node still shows `nvidia.com/31c2: 4` allocatable, so up to 4 GPUs per VM
(theoretical) or several single/dual-GPU VMs are possible.

## 6.6 Bandwidth, P2P and soak validation (2-GPU VM)

All numbers measured 2026-11-01 in the 2-GPU VM, driver 615.71.10 / CUDA 13.4.

**ECC (Red Hat documented check):** both GPUs report `ECC Mode: Enabled`
(current and pending); persistence mode enabled.

**nvbandwidth v0.10** (NVIDIA's current tool — the old `bandwidthTest`/
`p2pBandwidthLatencyTest` were dropped from cuda-samples and don't compile
against CUDA 13):

| Test | Per GPU | Notes |
|---|---|---|
| H2D memcpy (CE) | **211 GB/s** | far above the emulated PCIe x16 link — traffic uses the coherent C2C path, proving the post-fix coherent path is active |
| D2H memcpy (CE) | **193 GB/s** | same |
| H2D/D2H bidirectional | ~171 GB/s | |
| Local read (SM) | **7.6 TB/s** | near HBM3e spec |
| Local write (SM) | 6.5 TB/s | |
| Local copy (memcpy / SM / TMA) | 3.3 / 3.2 / 2.5 TB/s | |
| **GPU↔GPU (CE)** | **770–776 GB/s** | hardware NVLink between the two passed-through GPUs |
| GPU↔GPU latency (TMA) | **1.75 µs** | |

Cross-check: a small `cudaMemcpyPeer` loop (runtime path, no copy engine)
measured ~52 GB/s — a different, host-staged path. Kernel-based peer access
(`streamOrderedAllocationP2P`) works directly, as shown in 6.5.

**gpu-burn soak** (`wilicc/gpu-burn`, 120 s, both GPUs, ~254 GiB HBM used
per GPU): both GPUs **OK** (result matrices verified), SM utilization 100%,
power ~815/843 W (of 1400 W), 47/49 °C, memory clocks 3996 MHz, **zero ECC
errors, zero power/thermal violations**, clean return to idle.

Build notes: gpu-burn's binary is `gpu_burn` and needs
`LD_LIBRARY_PATH=/usr/local/cuda/lib64` at runtime; nvbandwidth builds with
`-DCMAKE_CUDA_ARCHITECTURES=103`. CUDA 13 removed `cudaEnablePeerAccess` —
peer access is automatic where supported, so `cudaMemcpyPeer` and
`cudaDeviceCanAccessPeer` are used directly.

---

# Troubleshooting log (everything that was hit)

| Symptom | Root cause | Fix |
|---|---|---|
| NodePool stuck: `cluster-config-api: Exec format error` | aarch64-only release image; payload generated on x86_64 hub | use `ocp-release:4.22.0-multi` |
| HostedCluster rejected base domain | `172.16.2.132.sslip.io` invalid DNS label | use `172-16-2-132.sslip.io` |
| VM fails to start: `failed to setup container for group 47: No available IOMMU models` | `vfio_iommu_type1` not loaded (legacy launcher) | DaemonSet in `03-gpu-operator/05-...`, or use iommufd |
| `Unknown PCI header type 127` on GPU config reads | GPU runtime-suspended while vfio-bound | `power/control=on` (same DaemonSet) |
| `NVRM: BAR0 is 0M @ 0x0`, BARs "can't assign; no space" (4.21) | QEMU virt 512 GiB MMIO window < 520 GiB of GB300 BARs | 4.21: hook sidecar `-machine highmem-mmio-size=16T`; **4.22: nothing — AIE sizes pcihole64=2TiB natively** |
| `kmemsysSetupCoherentCpuLink: Failed to get coherent GPU memory base address` (4.21) | 4.21 VM lacks the Grace C2C coherent-memory ACPI description | 4.21 only: `RMForceBarPath=1`; **4.22: nothing — coherent path works** |
| **4.22:** `kbusVerifyBar2_GH100: MMUTest BAR0 window offset 0x70e000 returned garbage 0x0`, `RmInitAdapter failed (0x24:0x72:1298)`, nvidia-smi "No devices were found", `cuInit: 999` | **the two 4.21 workarounds carried over**: (a) MMIO hook sidecar overriding AIE's native pcihole64, (b) `RMForceBarPath=1` forcing the BAR path | remove the hookSidecars annotation from the VM **and** delete `/etc/modprobe.d/nvidia-forcebarpath.conf`, then reboot the guest |
| `nvidia-smi: command not found` after driver install | binary is in `nvidia-driver-cuda`, not `nvidia-driver` | install `nvidia-driver-cuda-<same version>` |
| DKMS module not built for running kernel | dnf pulled a newer `kernel-core` than the devel package | pin `kernel-64k-devel`/`kernel-devel-matched` to `$(uname -r)` |
| `modprobe -r nvidia` hangs | unload hangs after failed probe | reboot the guest |
| `virtctl ssh` times out; `guest-exec` disabled | masquerade networking + libvirt agent allowlist | NodePort SSH service (`04-vm/03-...`) |
| CDI upload: `open /scratch/tmpimage: permission denied` | local-hostPath dirs are root-owned with `var_t` label | on the node: `chown 107:107 <dir>` **and** `chcon -t container_file_t <dir>` |
| upload DataVolume stuck Pending: `no volume plugin matched name: kubernetes.io/no-provisioner` | CDI requests ~13% above nominal PVC size (filesystem overhead) | size the PV above `requested storage` in the PVC spec |
| SSH to NodePort: `REMOTE HOST IDENTIFICATION HAS CHANGED` | NodePort previously served a different VM (new host key) | use a dedicated `UserKnownHostsFile` per VM |
| `virtctl image-upload` fails: `no such file or directory` | `~` not expanded in `--image-path` | pass an absolute path |
| GPU operator reports `failed to retrieve GPU node OS info` (non-passthrough use) | NFD not running on guest cluster | label node manually: `feature.node.kubernetes.io/system-os_release.ID=rhel`, `.VERSION_ID=9.8` |
| `run_tests.py`: `No executables found!` | binaries only land in `build/bin/<arch>/<os>/<type>` after `cmake --install`; or only 2 targets built | `cmake --build build -j$(nproc) && cmake --install build`, or use `--dir ./build/cpp` |

# References

* OpenShift Virtualization docs — PCI passthrough
  (`PermittedHostDevices`, hostDevices/gpus in VMI spec)
* NVIDIA GPU Operator — KubeVirt / vm-passthrough sandbox mode
  (docs.nvidia.com/datacenter/cloud-native/gpu-operator — "GPU Operator with
  OpenShift Virtualization")
* Red Hat Enterprise Linux for NVIDIA 26.03 — guest configuration
  (docs.redhat.com: "Configure a guest operating system for GPU workloads",
  "Install NVIDIA kernel and user mode drivers on a guest")
* HyperShift docs — Agent platform, multi-arch release images
  (hypershift.pages.dev)
* QEMU `virt` machine, `highmem-mmio-size` machine property (hw/arm/virt.c)
* NVIDIA open-gpu-kernel-modules — `RMForceBarPath`
  (src/nvidia/interface/nvrm_registry.h), `kmemsysSetupCoherentCpuLink`
  (src/nvidia/src/kernel/gpu/mem_sys/kern_mem_sys.c)
* NVIDIA nvbandwidth (github.com/NVIDIA/nvbandwidth),
  gpu-burn (github.com/wilicc/gpu-burn),
  CUDA samples (github.com/NVIDIA/cuda-samples)
* Internal QE reference for GB300 passthrough on this stack:
  NVIDIA-1401 / NVIDIA-1408 (CNV 4.22.12, OCP4NV 26.03 kernel 259.26,
  guest driver 615.62, Oct 2026) — working but not certified
