# Archive — superseded and historical material

Nothing in this directory is needed to rebuild the validated state. The
authoritative build guide and the **final, working configurations** live in
[`Final/`](../Final/README.md) (manifests under `Final/configs/`).

Every custom component, current or retired, is indexed with the reason it
exists (or was retired) in
[`Final/appendix-b-custom-components.md`](../Final/appendix-b-custom-components.md);
incidents and root causes are in
[`Final/appendix-a-issues-and-workarounds.md`](../Final/appendix-a-issues-and-workarounds.md).

| Path | What it is | Why it's here and not in `Final/configs/` |
|---|---|---|
| `03-gpu-operator/05-`, `06-` | VFIO type1 loader / GPU anti-suspend DaemonSets | superseded by the `devices.kubevirt.io/iommufd` resource request |
| `04-vm-legacy/` | single-VM-era manifests (tray-11) + `gpu-mmio-hook.sh` | superseded by the per-tray VMs in `Final/configs/04-vm/tray-{3,12}/`; the MMIO hook is actively harmful on CNV 4.22 + AIE (root cause of the GPU-init regression) |
| `05-guest-driver/nvidia-forcebarpath.conf` | guest `RMForceBarPath=1` modprobe conf | CNV 4.21 only; breaks GPU init on 4.22 |
| `07-rdma/04-sriov-nodepolicy.yaml` | SR-IOV node policy (retry variant) | the policy layer reboot-loops these workers; the final state runs **no** SR-IOV policies — replaced by the VF-supply DaemonSet |
| `07-rdma/06-sriovdp-standalone.yaml` | standalone SR-IOV device plugin | superseded by the cx8-shim device plugin |
| `07-rdma/07-bf3-shim-ds.yaml` | BF-3 shim DaemonSet | BF-3 VF attach to the VM pair was superseded by the CX-8 rails (shim source is shared and lives in `Final/configs/07-rdma/bf3-shim/`) |
| `07-rdma/10-`, `11-` | two-rail VM + shim patches | superseded by the four-rail profile (`Final/configs/07-rdma/12-`, `13-`); kept as documented rollback targets |
| `07-rdma/*-validation-2026-11-05.md`, `recovery-2026-11-05.md` | dated validation and recovery reports | historical evidence, superseded by the consolidated Chapter 8 results; the four-rail report carries the 4-vCPU addendum |
| `discovery/` | node/BMC discovery JSON (tray-12, tray-3) | troubleshooting evidence from the build, not configuration |
