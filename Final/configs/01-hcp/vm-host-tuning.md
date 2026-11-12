# GPU VM worker tuning in HCP

These ConfigMaps are management-cluster inputs to the existing
`clusters/my-hosted-cluster` NodePool. Do not apply the embedded KubeletConfig or
MachineConfig directly to a guest MachineConfigPool — the guest has no MCO and
ignores them. The general wrapper/reference mechanism is documented in
`02-hcp-hosted-cluster.md` §2.5, with a generic example in
`machineconfig-example-configmap.yaml`.

Red Hat docs (OCP 4.22):
- Configuring node pools (NodePool.spec.config mechanism):
  https://docs.openshift.com/container-platform/4.22/hosted_control_planes/hcp-machine-config.html#configuring-node-pools-for-hcp_hcp-machine-config
- Referencing the kubelet configuration in node pools:
  https://docs.openshift.com/container-platform/4.22/hosted_control_planes/hcp-machine-config.html#hcp-kubeconf-nodepool_hcp-machine-config

- `vm-host-kubelet-config.yaml`: static CPU Manager, restricted topology policy,
  and one CPU reserved in total (500m kube-reserved + 500m system-reserved).
  Preserve `max-allowable-numa-nodes: "34"`: the GPUs expose additional
  memory-only NUMA nodes, so the default eight-node topology limit is insufficient.
- `vm-host-hugepages.yaml`: persistent 2 MiB hugepages before kubelet startup,
  32768 pages / 64 GiB per CPU-bearing NUMA node. Memory-only GPU NUMA nodes are
  deliberately excluded. The validated trays have two CPU-bearing NUMA nodes.

Retain the existing `custom-machineconfig-v2` OS-image reference when appending
these two references to `NodePool.spec.config`. Never replace that list blindly
or reapply the historical one-replica NodePool manifest from this repository.
Treat a configuration-hash change as maintenance for both workers.

## Maintenance safeguards

1. Save the live NodePool, referenced configuration, VM, PVC/PV and effective
   kubelet configuration privately. Keep kubeconfig/Secret contents out of exports.
2. Verify both UEFI boot orders start with the local Red Hat disk and that the
   intended RHCOS 10.2 deployment is already booted/default. Do not allow a reboot
   to select the lab's unrelated Ubuntu autoinstall PXE target.
3. Record the original VM runStrategy, flush guest writes, stop it gracefully,
   and wait for its VMI to disappear before making an offline disk copy. Preserve
   the original root PVC/PV and disk file; this is not a live-migration workflow.
4. Use an offline XFS reflink copy as a local rollback copy if supported. This
   protects against logical maintenance mistakes, not loss of the physical disk.
5. Validate the ConfigMaps before attaching them. Roll out one worker at a time
   using the NodePool's InPlace controller; do not manually reboot both at once.
6. Verify both workers return Ready, static CPU Manager/restricted topology are
   effective, hugepages are available on CPU NUMA nodes, and the original GPU
   resources are restored. Restore the VM's original runStrategy and verify its
   two GPUs/CUDA before creating the second VM.

OpenShift's normal `kubelet-cleanup.service` removes CPU/memory manager checkpoints
after reboot. Do not manually delete checkpoints or restore a stale checkpoint
while kubelet is running. Diagnose a failed rollout before taking recovery action.

## Rollback

Use the recorded NodePool configuration references as the rollback authority.
Remove only the newly added references (preserve the OS image and any other
pre-existing inputs), then allow reconciliation to converge. Removing these
inputs can itself trigger disruptive reconciliation; it does not reverse a disk
write or automatically restore previously manual kubelet/hugepage tuning.

The source worker had pre-existing manual tuning. Its effective configuration
and hugepage distribution must be retained in the private backup so it can be
recovered deliberately if controller-managed tuning fails. Restore the original
VM runStrategy only after its host resources are sufficient again. Do not delete
the source VM/PVC/PV or use a NodePool scale-down as a tuning rollback.
