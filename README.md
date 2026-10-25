# HCP + OpenShift Virtualization + GB300 GPU Passthrough on ARM64

End-to-end reference for running an NVIDIA GB300 (Grace-Blackwell) GPU inside a
KubeVirt virtual machine on a hosted (HCP / HyperShift) OpenShift cluster.

The setup was built and validated on a GB300 NVL72 system:

| Component | Value |
|---|---|
| Hub cluster | OpenShift 5.0.0-rc.6, x86_64, node `control-plane-12` (172.16.2.132) |
| Guest (hosted) cluster | OpenShift 4.22.0 HCP, Agent platform, multi-arch payload |
| Guest worker node | `compute-tray-11` (172.16.2.113), RHCOS 9.8, kernel 5.14.0-687, arm64, 4x GB300 |
| Virtualization | OpenShift Virtualization (CNV) 4.21.17, virt-launcher QEMU 10.1.0 |
| GPU operator | NVIDIA GPU Operator 26.7.2, vm-passthrough (sandbox) mode |
| Guest VM | RHEL 10.2 ARM64 (`rhel4nv-26.03-aarch64-kvm.qcow2`), kernel 6.12.0-265.29.el10nv |
| Guest driver | NVIDIA open kernel modules 615.71.10 |

Final result — `nvidia-smi` inside the VM:

```
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 615.71.10              KMD Version: 615.71.10     CUDA UMD Version: 13.4     |
+=========================================+========================================+======+
|   0  NVIDIA GB300                   On  |       00000000:0A:00.0 Off |                    0 |
| N/A   29C    P0            180W / 1400W |       0MiB / 284208MiB |      0%      Default |
+-----------------------------------------------------------------------------------------+
```

## Architecture

```text
x86_64 hub cluster (OpenShift 5.0)
  └─ HostedControlPlane pods (my-hosted-cluster, namespace: clusters)
       └─ ARM64 guest worker node (compute-tray-11, booted via Agent/discovery ISO)
            ├─ OpenShift Virtualization (CNV / KubeVirt)
            │    └─ VM gpu-vm (RHEL 10.2 arm64)
            │         └─ GB300 passed through via VFIO/iommufd
            │              ├─ 16 TiB guest PCI MMIO window (hook sidecar)
            │              └─ NVIDIA driver 615.71.10 + RMForceBarPath=1
            └─ NVIDIA GPU operator (vm-passthrough mode)
                 ├─ vfio-manager: binds GB300s to vfio-pci
                 └─ sandbox-device-plugin: advertises nvidia.com/31c2
```

Division of labor for GPU passthrough:

* **GPU operator (node side)** — binds the GPU to `vfio-pci` and advertises it as
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
02-cnv/                          OpenShift Virtualization operator + HyperConverged
03-gpu-operator/                 GPU operator, vm-passthrough policy, CNV device whitelist
04-vm/                           The GPU VM: DataVolume, VM, hook sidecar, SSH service
05-guest-driver/                 In-guest NVIDIA driver + ARM64-specific fixes
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
  sshAuthorizedKey: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICS5eKZo0aBFc0JbS4fP76cCyH+cy0HjSHi9XYeFlMOy nvidia@rh-gb300nvl72-v2-bastion
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
# compute-tray-11  Ready    worker  ...   v1.35.8
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
  startingCSV: kubevirt-hyperconverged-operator.v4.21.17
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
kernel `6.12.0-265.29.el10nv`). Create the DataVolume and upload the qcow2:

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
UPLOAD_ROUTE=$(oc get route -n cdi cdi-uploadproxy -o jsonpath='{.spec.host}')
virtctl image-upload dv gpu-vm-rhel-root -n gpu-vm-test \
  --size=56Gi --storage-class=local-hostpath \
  --image-path=rhel4nv-26.03-aarch64-kvm.qcow2 \
  --uploadproxy-url=https://$UPLOAD_ROUTE --insecure \
  --access-mode=ReadWriteOnce
```

## 4.3 MMIO window hook sidecar (the ARM64 + huge-BAR fix)

The GB300 exposes ~520 GiB of PCI BARs (BAR2 = 512 GiB via Resizable BAR).
The QEMU `virt` machine's default 64-bit MMIO window is only 512 GiB, so the
guest kernel cannot assign the BARs and the NVIDIA driver fails with
`NVRM: BAR0 is 0M @ 0x0`.

Deploy the hook as a ConfigMap — `04-vm/gpu-mmio-hook.sh`:

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

`04-vm/02-gpu-vm.yaml` — the exact spec that works:

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
      annotations:
        hooks.kubevirt.io/hookSidecars: '[{"args":["--version","v1alpha2"],"configMap":{"name":"gpu-mmio-hook","key":"onDefineDomain.sh","hookPath":"/usr/bin/onDefineDomain"}}]'
      labels:
        kubevirt.io/domain: gpu-vm
        kubevirt.io/vm: gpu-vm
    spec:
      nodeSelector:
        kubernetes.io/hostname: compute-tray-11
      domain:
        machine:
          type: virt
        cpu:
          cores: 2
          dedicatedCpuPlacement: true
        resources:
          requests:
            memory: 16Gi
          limits:
            devices.kubevirt.io/iommufd: "1"
        memory:
          guest: 16Gi
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
            - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICS5eKZo0aBFc0JbS4fP76cCyH+cy0HjSHi9XYeFlMOy nvidia@rh-gb300nvl72-v2-bastion
```

Notes:

* `devices.kubevirt.io/iommufd: "1"` makes virt-launcher use the kernel iommufd
  VFIO container (requires a libvirt >= 12.2 build) — this is what obsoletes
  the Phase 3.4 type1 workaround.
* `gpus: deviceName: nvidia.com/31c2` is the passthrough request.
* The hook annotation must reference the ConfigMap from 4.3.
* Changing the hook or any qemu-level option requires a full VM stop/start —
  the QEMU command line is fixed for the life of the process.

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

Inside the VM (RHEL 10.2 ARM64). Repo prerequisites — the `rhel4nv` image ships
these already; on a stock RHEL image:

```bash
# 1. Public NVIDIA CUDA repo (nvidia-driver*, nvidia-driver-cuda, kmod)
sudo dnf config-manager --add-repo \
  https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/cuda-rhel10.repo

# 2. The rhel-10-for-aarch64-nvidia-rpms driver channel (kmod-nvidia-open-dkms,
#    nvidia-modprobe, el10nv kernel) comes from a Red Hat subscription
#    (RHEL for NVIDIA entitlement, in /etc/yum.repos.d/redhat.repo):
dnf repolist | grep nvidia    # verify it is attached
```

Then install, pinning kmod and userspace to the same version:

```bash
DRIVER_VERSION=615.71.10

# Kernel module + base userspace
sudo dnf install -y kmod-nvidia-open-dkms-${DRIVER_VERSION} \
                    nvidia-driver-${DRIVER_VERSION} \
                    nvidia-modprobe-${DRIVER_VERSION}

# nvidia-smi ships here (NOT in nvidia-driver or the kmod package)
sudo dnf install -y nvidia-driver-cuda-${DRIVER_VERSION}
```

Version pinning matters: the kmod and userspace versions must match exactly.

## 5.1 Fix 1 — guest PCI MMIO window (applied in Phase 4.3)

If BARs cannot be assigned you will see, inside the VM:

```text
pci 0000:0a:00.0: BAR 2 [mem size 0x8000000000 64bit pref]: can't assign; no space
NVRM: This PCI I/O region assigned to your NVIDIA device is invalid:
NVRM: BAR0 is 0M @ 0x0 (PCI:0000:0a:00.0)
nvidia 0000:0a:00.0: probe with driver nvidia failed with error -1
```

That is the symptom of the missing `highmem-mmio-size` machine property — see
Phase 4.3. Verify in the guest after boot:

```bash
sudo dmesg | grep "0a:00.0.*assigned"      # all three BARs assigned
sudo lspci -vvv -s 0a:00.0 | grep Region   # non-empty Region lines
```

## 5.2 Fix 2 — RMForceBarPath (Grace C2C coherent link)

With BARs assigned, the driver still fails on a Grace host because the RM
expects the GPU's coherent (NVLink-C2C) memory window to be described by
platform firmware (ACPI DSD) — which a VM does not have:

```text
NVRM: GPU0 kmemsysSetupCoherentCpuLink_IMPL: Failed to get coherent GPU memory base address
NVRM: GPU0 RmInitNvDevice: *** Cannot initialize the device
```

The driver's escape hatch is the `RMForceBarPath` registry key ("disable
coherent path CPU->Nvlink/C2C->FB and force BAR path",
`src/nvidia/interface/nvrm_registry.h` in open-gpu-kernel-modules):

```bash
# 05-guest-driver/nvidia-forcebarpath.conf -> /etc/modprobe.d/
echo 'options nvidia NVreg_RegistryDwords="RMForceBarPath=1"' | \
  sudo tee /etc/modprobe.d/nvidia-forcebarpath.conf

sudo reboot    # required — see warning below
```

**Warning:** do not attempt `modprobe -r nvidia` to reload after a failed
probe — the unload hangs in this state. Reboot the guest.

After reboot, success looks like:

```text
NVRM: GPU0 kmemsysSetupCoherentCpuLink_IMPL: Force disabling NVLINK/C2C mappings through regkey.
NVRM: loading NVIDIA UNIX Open Kernel Module for aarch64  615.71.10  Release Build
```

## 5.3 Verify

```bash
nvidia-smi
lspci -nnk -s 0a:00.0 | grep "Kernel driver in use"   # nvidia
```

---

# Troubleshooting log (everything that was hit)

| Symptom | Root cause | Fix |
|---|---|---|
| NodePool stuck: `cluster-config-api: Exec format error` | aarch64-only release image; payload generated on x86_64 hub | use `ocp-release:4.22.0-multi` |
| HostedCluster rejected base domain | `172.16.2.132.sslip.io` invalid DNS label | use `172-16-2-132.sslip.io` |
| VM fails to start: `failed to setup container for group 47: No available IOMMU models` | `vfio_iommu_type1` not loaded (legacy launcher) | DaemonSet in `03-gpu-operator/05-...`, or use iommufd |
| `Unknown PCI header type 127` on GPU config reads | GPU runtime-suspended while vfio-bound | `power/control=on` (same DaemonSet) |
| `NVRM: BAR0 is 0M @ 0x0`, BARs "can't assign; no space" | QEMU virt 512 GiB MMIO window < 520 GiB of GB300 BARs; `-global gpex-pcihost.x-above-4g-mmio-size` is a no-op | hook sidecar injecting `-machine highmem-mmio-size=16T` |
| `kmemsysSetupCoherentCpuLink: Failed to get coherent GPU memory base address` | VM lacks the Grace C2C coherent-memory ACPI description | `NVreg_RegistryDwords="RMForceBarPath=1"` + guest reboot |
| `nvidia-smi: command not found` after driver install | binary is in `nvidia-driver-cuda`, not `nvidia-driver` | install `nvidia-driver-cuda-<same version>` |
| `modprobe -r nvidia` hangs | unload hangs after failed probe | reboot the guest |
| `virtctl ssh` times out; `guest-exec` disabled | masquerade networking + libvirt agent allowlist | NodePort SSH service (`04-vm/03-...`) |
| GPU operator reports `failed to retrieve GPU node OS info` (non-passthrough use) | NFD not running on guest cluster | label node manually: `feature.node.kubernetes.io/system-os_release.ID=rhel`, `.VERSION_ID=9.8` |

# References

* OpenShift Virtualization docs — PCI passthrough
  (`PermittedHostDevices`, hostDevices/gpus in VMI spec)
* NVIDIA GPU Operator — KubeVirt / vm-passthrough sandbox mode
* QEMU `virt` machine, `highmem-mmio-size` machine property (hw/arm/virt.c)
* NVIDIA open-gpu-kernel-modules — `RMForceBarPath`
  (src/nvidia/interface/nvrm_registry.h), `kmemsysSetupCoherentCpuLink`
  (src/nvidia/src/kernel/gpu/mem_sys/kern_mem_sys.c)
