# 3 — OpenShift Virtualization (CNV) + AIE

Install CNV on the **managed (hosted) cluster** and enable AIE, which is
mandatory for GB300 passthrough on CNV 4.22. Manifests: `configs/02-cnv/`.

## 3.1 Install the CNV operator

```bash
export KUBECONFIG=$PWD/guest-kubeconfig
oc apply -f 02-cnv/          # namespace, operatorgroup, subscription, HyperConverged
oc get csv -n openshift-cnv  # kubevirt-hyperconverged-operator.v4.22.12 Succeeded
```

The subscription is pinned to `stable` with
`startingCSV: kubevirt-hyperconverged-operator.v4.22.12`.

## 3.2 HCP placement: schedule CNV on workers only

HCP workers do not carry the `master` label, while some CNV deployments
default to master/control-plane affinity. Configure explicit worker
placement instead of labeling workers as masters:

```bash
oc patch hyperconverged kubevirt-hyperconverged -n openshift-cnv \
  --type=merge --patch-file=02-cnv/patches/hcp-worker-placement.yaml
oc patch subscription kubevirt-hyperconverged -n openshift-cnv \
  --type=merge --patch-file=02-cnv/patches/hcp-subscription-placement.yaml
```

⚙ **CUSTOM CONFIGURATION — worker-only placement patches.** *Problem:* on a
hosted control plane the masters are pods on the hub; the guest workers
have no master label, so CNV operator/infrastructure deployments with
default master affinity stay Pending. *Fix:* the HCO v1
`spec.deployment.nodePlacements.infra` patch and the equivalent OLM
Subscription patch select `node-role.kubernetes.io/worker: ""` explicitly.
An earlier temporary workaround (labeling workers as masters) is
**deprecated and was removed** — do not repeat it; see Appendix A.

Wait for all CNV deployments to roll out and verify their affinity no
longer depends on master labels.

## 3.3 Enable AIE

AIE (Accelerated Infrastructure Enablement) provides the NVIDIA-tuned
`virt-launcher-4-nv-rhel10` image, the iommufd device plugin, and the
vSMMUv3/NUMA/pcihole64 generation that GB300 passthrough depends on.

```bash
oc annotate hyperconverged kubevirt-hyperconverged -n openshift-cnv \
  hco.kubevirt.io/deployAIE=true
```

Verify the AIE operands:

```bash
oc get deployment kubevirt-aie-webhook -n openshift-cnv
oc get daemonset iommufd-device-plugin -n openshift-cnv
```

## 3.4 Select the NV launcher image

The AIE webhook swaps in the NVIDIA-tuned virt-launcher for VMs requesting
GPU resources. Extract the image from the installed CSV and wire it into
the webhook ConfigMap:

```bash
CSV=$(oc get csv -n openshift-cnv -o name | grep kubevirt-hyperconverged-operator | sed 's|.*/||')
LAUNCHER_NV_IMAGE=$(oc get csv $CSV -n openshift-cnv -o json \
  | jq -r '.spec.relatedImages[] | select(.name | test("virt-launcher-4-nv")) | .image')
# substitute into 02-cnv/04-aie-launcher-config.yaml, then:
oc apply -f 02-cnv/04-aie-launcher-config.yaml
```

The config's first rule matches VMs requesting `nvidia.com/31c2` (the GPU
resource from Chapter 4) — no VM label needed. A second labeled-VM rule is
a fallback for VMs without GPU resources.

**Do not use the two legacy CNV-4.21 workarounds** (MMIO-window hook
sidecar, guest `RMForceBarPath`). Both are actively harmful on 4.22 + AIE
and were the root cause of the GPU-init regression documented in
Appendix A. On 4.22, AIE sizes the guest PCI window (`pcihole64`)
natively and provides the Grace coherent-memory description.
