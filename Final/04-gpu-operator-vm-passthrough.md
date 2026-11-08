# 4 — GPU operator in vm-passthrough mode + device whitelist

The NVIDIA GPU Operator runs on the **managed cluster** and manages only
the node side: binding the GB300s to the VFIO variant driver and
advertising them as a KubeVirt-consumable resource. It installs **no node
driver** — everything inside the VM is manual (Chapter 6). Manifests:
`configs/03-gpu-operator/`.

## 4.1 Install the operator

```bash
oc apply -f 03-gpu-operator/00-gpu-operator-namespace.yaml
oc apply -f 03-gpu-operator/01-gpu-operator-operatorgroup.yaml
oc apply -f 03-gpu-operator/02-gpu-operator-subscription.yaml
# pinned: gpu-operator-certified.v26.7.2 from certified-operators
```

## 4.2 ClusterPolicy — KubeVirt vm-passthrough mode

```bash
oc apply -f 03-gpu-operator/03-gpu-cluster-policy.yaml
```

The key spec (full file in the repo):

```yaml
spec:
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
  # dcgm, dcgmExporter, devicePlugin, gfd, toolkit, validator: enabled
```

Expect these pods and **no** `nvidia-driver-daemonset`:

```text
nvidia-vfio-manager-...
nvidia-sandbox-device-plugin-daemonset-...
nvidia-sandbox-validator-...
gpu-operator-...
```

`nvidia-vfio-manager` binds each GB300 to `nvgrace_gpu_vfio_pci` (the
Grace/Blackwell VFIO variant driver); the sandbox device plugin advertises
the resource.

## 4.3 Whitelist the GPU as a CNV permitted host device

Without this, KubeVirt refuses to schedule VMs consuming the resource:

```bash
oc apply -f 03-gpu-operator/04-cnv-permitted-host-devices.yaml
```

```yaml
spec:
  permittedHostDevices:
    pciHostDevices:
    - pciDeviceSelector: 10de:31c2
      resourceName: nvidia.com/31c2
      externalResourceProvider: true
```

⚙ **CUSTOM CONFIGURATION — `externalResourceProvider: true`.** *Problem:*
the resource is advertised by the GPU operator's sandbox device plugin,
not by KubeVirt's internal plugin; without the flag KubeVirt tries to
discover/advertise the devices itself and fails. *Fix:* the flag tells
KubeVirt to trust the external provider. This is the documented pattern
for operator-managed resources (the same pattern the NIC shims in
Chapter 7 replicate for CX-8 VFs).

Verify on each worker:

```bash
oc get node compute-tray-3 -o json | jq '.status.allocatable["nvidia.com/31c2"]'
# "4"
```

## 4.4 (Legacy, not applied) VFIO type1 fix

`03-gpu-operator/05-gpu-vfio-type1-fix.yaml` + `06-...-rbac.yaml` load
`vfio_iommu_type1` and keep vfio-bound GPUs out of runtime suspend. **Not
needed** on this stack: the VM requests the iommufd resource (Chapter 5)
and libvirt ≥ 12.2 uses the iommufd container. Kept in the repo for older
launcher builds only.
