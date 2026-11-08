# Appendix C — Hub cluster setup (reference)

> **Status: reference, not re-validated.** The hub used throughout this build
> (`hub-node-1`, x86_64 OpenShift 5.0.0-rc.6) already existed when the work
> started, so these steps were **not re-executed** during validation. They
> document what the hub must look like before
> [Chapter 2](02-hcp-hosted-cluster.md), using the standard, version-stable
> procedure. Adapt versions to your hub release and consult the official
> docs for anything version-specific.

The hub is the management cluster that hosts the control planes of the
hosted (HCP) clusters. For this build it was a **single x86_64 node** with:

| Requirement | This build's value |
|---|---|
| OpenShift | 5.0.0-rc.6, x86_64, single node (`hub-node-1`, 172.16.2.132) |
| MultiCluster Engine | standalone MCE (ACM not installed) |
| Default StorageClass | `lvms-vg1` (LVMS operator, local disks) |
| API exposure for hosted clusters | NodePort on the hub address (no MetalLB) |

## C.1 Install the hub OpenShift cluster

Any standard x86_64 OpenShift install works; the hub carries no ARM64
workload. For a bare-metal lab, the **agent-based installer** is the
simplest path to a single-node or compact hub:

1. Download the OpenShift installer + `oc` for your release from the
   [OpenShift console/downloads](https://console.redhat.com/openshift/downloads)
   (x86_64).
2. Generate agent-based install manifests (`openshift-install agent create
   install-config ...` or via the web console's wizard), with:
   - **DNS/`api-int`** reachable from your bastion and from the workers —
     the workers must resolve the hosted cluster's API on the hub address.
   - A **routable hub address** (here 172.16.2.132) that will carry the
     hosted-cluster NodePorts.
   - NTP and a registry/pull-secret story as usual.
3. Boot the hub, wait for `oc get clusterversion` →
   `Progressing: False`, and save the kubeconfig as `kubeconfig_hub`.

Size note: the hub hosts one hosted control plane per HCP cluster
(operators + a single etcd replica in this SingleReplica build) — small,
but give it local disks with room for the etcd PVs.

## C.2 Install MultiCluster Engine and enable Hosted Control Planes

HCP (HyperShift) is delivered through the **MultiCluster Engine (MCE)**
operator — ACM is not required:

1. In the hub console's OperatorHub, install **MultiCluster Engine**
   (namespace `multicluster-engine`, latest stable channel matching your
   hub release). Via CLI:

   ```bash
   export KUBECONFIG=$PWD/kubeconfig_hub
   oc create namespace multicluster-engine
   oc create -f - <<'EOF'
   apiVersion: operators.coreos.com/v1alpha1
   kind: Subscription
   metadata:
     name: multicluster-engine
     namespace: multicluster-engine
   spec:
     name: multicluster-engine
     source: redhat-operators
     sourceNamespace: openshift-marketplace
   EOF
   ```

2. Create the `MultiClusterEngine` custom resource (the operator's default
   is sufficient for standalone use):

   ```bash
   oc create -f - <<'EOF'
   apiVersion: multicluster.openshift.io/v1alpha1
   kind: MultiClusterEngine
   metadata:
     name: multicluster-engine
     namespace: multicluster-engine
   spec: {}
   EOF
   ```

3. On current MCE releases Hosted Control Planes are **on by default** and
   the HyperShift operator deploys into `multicluster-engine`. On older MCE
   you had to opt in (the `hypershift-preview`/HostedControlPlanes feature
   gate) — if the check below fails, consult the MCE docs for your version.

4. **Verify:**

   ```bash
   oc get mce multicluster-engine -o jsonpath='{.status.phase}'   # Available
   oc get deployment hypershift -n multicluster-engine            # Available
   ```

## C.3 Default StorageClass for the hosted etcd

Every hosted cluster consumes RWO block PVs for its etcd (this build:
8 Gi, `lvms-vg1`). The hub needs a **default** StorageClass before
`hcp create cluster` runs:

- **LVMS example (bare metal, local disks):** install the **LVM Storage**
  operator from OperatorHub, create an `LVMCluster` CR covering the hub's
  disks, and mark the resulting class (`lvms-vg1`) as default:

  ```bash
  oc patch storageclass lvms-vg1 -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
  oc get storageclass                          # (default) marker present
  ```

- Any other RWO block storage (ODF, NFS-provisioned block, iSCSI) works;
  the class name is passed as `--etcd-storage-class` in Chapter 2.

## C.4 The `hcp` CLI

`hcp` is the HyperShift CLI used in [Chapter 2](02-hcp-hosted-cluster.md)
to create hosted clusters. It must **match the hub's MCE/HCP version**:

- From the MCE version that shipped your HCP: download the matching
  `hypershift`/`hcp` binary from the
  [openshift/hypershift releases](https://github.com/openshift/hypershift/releases)
  (linux/amd64 for the bastion), or via the MCE console's CLI downloads.
- Verify:

  ```bash
  ./hcp version    # should report a build matching the hub's hypershift operator
  ```

Keep `oc` and `virtctl` at versions compatible with the hosted cluster
(4.22 here); `virtctl` comes from the CNV release mirrored on the console
downloads page.

## C.5 Readiness checklist (gate before Chapter 2)

```bash
export KUBECONFIG=$PWD/kubeconfig_hub
oc get clusterversion                                   # Progressing: False
oc get mce multicluster-engine -o jsonpath='{.status.phase}'   # Available
oc get deployment hypershift -n multicluster-engine     # Available
oc get storageclass                                     # one (default) RWO class
./hcp version                                           # matches hub
```

All green → continue with [Chapter 2](02-hcp-hosted-cluster.md).
