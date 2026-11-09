# RHOAI on HCP — Work Plan

**Task:** Deploy and run representative training + inference workloads using
Red Hat OpenShift AI (RHOAI); validate GPU acceleration end to end.

**Success criteria:** RHOAI installed; training and inference workloads run
to completion on GPUs; MIG-/DRA-backed allocations honored; results within
expected performance envelope.

**Decisions (locked 2026-11-09):**

| Decision | Choice |
|---|---|
| GPU nodes | **`compute-tray-11` only** (VM halted, 4× GB300 idle — zero impact on the validated CNV/RDMA state on tray-3/12) |
| Training workload | **ResNet-50 / vision** (PyTorchJob, DDP, synthetic data) |
| Inference | **vLLM via KServe** |
| CNV involvement | **None** — bare-metal GPU pods on the existing HCP hosted cluster |

**Execution context:** existing HCP environment (hub 5.0.0-rc.6, hosted
4.22.19). Cluster access from the bastion
(`~/workspaces/skaufman/task_hcp/`: `kubeconfig_hub`, guest kubeconfig,
`hcp` CLI). No CNV components are touched; tray-3/12 stay as validated.

**Reuse inventory:**

- HCP cluster + workers, worker tuning (hugepages, topology manager)
- `rdma-gb300.html` (internal guide): the bare-metal GB300 pod playbook —
  NFD, host config, ACS/memlock considerations, SR-IOV RoCE rails
- GPU Operator 26.7.2 knowledge (currently vm-passthrough mode — see Phase 0)
- Repo discipline: evidence trees on the bastion, symptom→cause→fix defect
  appendix, validation-report format (see
  `reports/grace-blackwell-hcp-validation-report.md`)

---

## Phase 0 — GPU topology for tray-11 (critical path)

The GPU Operator currently runs in **vm-passthrough (sandbox) mode**:
host-side driver is not deployed; GPUs are consumed via vfio/iommufd by the
VMs. Bare-metal RHOAI pods need GPUs bound to the **`nvidia` driver on the
host** of tray-11. One ClusterPolicy cannot serve both modes, so:

1. **Inspect tray-11's current GPU state** (driver binding, IOMMU state,
   allocatable resources) — `oc debug node/compute-tray-11`, `lspci -k`,
   `oc describe node` resource capacity.
2. **Deploy host driver for tray-11 only**, preferred order:
   a. second GPU Operator instance (separate operator namespace) with a
      `ClusterPolicy` in standard mode, driver DaemonSet nodeSelector
      `kubernetes.io/hostname: compute-tray-11`; or
   b. ClusterPolicy 26.7.2 with per-component nodeSelectors if mixed
      topologies are supported in this version; or
   c. (last resort) manually installed host driver, unmanaged.
3. **Do not touch tray-3/12** — their vfio/VM state is the validated
   CNV+RDMA stack.
4. Host prep per the bare-metal guide: NFD labels confirmed, memlock
   ulimit, PCIe ACS assessment for GPU↔NIC P2P (bare-metal path now
   relevant, unlike the VM path).
5. **Exit criteria:** `nvidia.com/gpu: 4` allocatable on tray-11;
   `nvidia-smi` works from a test pod; tray-3/12 untouched (verify VMs
   still Running, RDMA resources intact).

## Phase 1 — RHOAI install

1. Install the **Red Hat OpenShift AI** operator (OLM) on the hosted
   cluster; create the `DataScienceCluster` CR.
2. Verify components: dashboard, notebook workbenches, **Training
   Operator** (PyTorchJob CRD), **KServe** serving runtime.
3. Smoke test: GPU notebook pod on tray-11 — `nvidia-smi` in-notebook, one
   small CUDA op.
4. **Exit criteria:** all RHOAI components Available; smoke-test PASS.

## Phase 2 — MIG-backed allocations

1. Discover GB300/Blackwell-Ultra MIG geometry at runtime
   (`nvidia-smi mig -lgip` on tray-11) — do not assume Hopper layouts.
2. Partition **one** GPU via the GPU Operator MIG manager
   (`nvidia.com/mig.config` node label); keep 3 GPUs whole for
   training/inference phases.
3. Run pods requesting specific MIG instances; verify:
   - allocation honored (pod sees exactly its instance's SMs/memory)
   - isolation between two pods on two instances of the same GPU
   - correct instance binding visible via `nvidia-smi`
4. Teardown MIG config and verify restoration to full GPU.
5. **Exit criteria:** MIG create/allocate/isolate/restore all PASS,
   evidence logged.

## Phase 3 — DRA-backed allocations (flagged risk)

1. Check DRA (structured parameters) availability: OCP 4.22 kubelet
   feature gates, GPU Operator DRA driver support for this version.
2. If available: validate a pod with `ResourceClaim`/`ResourceSlice`
   gets a GPU; repeat for a MIG instance via DRA; verify claim cleanup.
3. **If not available/GA on this stack:** document as blocked-on-platform
   (with the exact gate/driver gap), cover MIG + time-slicing as the
   delivered allocation mechanisms, mark DRA best-effort in the report.
4. **Exit criteria:** DRA allocation honored, or documented platform
   blocker with evidence.

## Phase 4 — Training: ResNet-50 (PyTorchJob)

1. **Baseline first:** single-GPU PyTorchJob, ResNet-50, synthetic data
   (no dataset logistics), fixed epoch/step count — record images/sec.
2. **4-GPU DDP run** (torchrun) on tray-11 — job RunsToCompletion,
   all 4 GPUs engaged (`nvidia-smi` during run), loss converges
   (synthetic task sanity), record images/sec aggregate.
3. **Performance envelope:** scaling efficiency = 4-GPU throughput ÷
   (4 × single-GPU) — require **≥ 75%**; compare per-GPU throughput
   against any known-good GB300 reference if available.
4. Optional stress: NVLink P2P check (`nvidia-smi topo`, torch
   `dist.all_reduce` bandwidth microbenchmark) to explain any gap.
5. **Exit criteria:** completed multi-GPU training run within envelope,
   logs + metrics archived to the bastion evidence tree.

## Phase 5 — Inference: vLLM via KServe

1. Deploy vLLM serving runtime (RHOAI KServe `ServingRuntime` +
   `InferenceService`) with an LLM that fits one GPU (candidate:
   Llama-3.1-8B-class or Qwen 7B-class; 283 GiB HBM per GB300 is ample).
2. Validate: GPU-scheduled pod, model loads, requests served, GPU
   utilization visible under load.
3. **Envelope:** fixed load profile (concurrency, input/output lengths) —
   tokens/sec throughput and TTFT/p99 latency recorded and required to
   hold steady over a sustained run (no degradation/leak).
4. **MIG-backed serving variant:** repeat once on a MIG instance to prove
   MIG allocations honored end-to-end in the serving path.
5. **Exit criteria:** serving runs within envelope on full GPU and on a
   MIG instance.

## Phase 6 — Report

Reuse the previous task's format (see
`reports/grace-blackwell-hcp-validation-report.md`):
environment/versions → per-phase tests + results → defects appendix
(symptom→cause→fix) → known limits → sign-off. New chapter set in this
directory (`rhoai/`) as the task's guide + configs, evidence trees on the
bastion under `maintenance/<timestamp>-<topic>/`.

### Success-criteria mapping

| Criterion | Phase |
|---|---|
| RHOAI installed | 1 |
| Training workload runs to completion on GPUs | 4 |
| Inference workload runs on GPUs | 5 |
| MIG-backed allocations honored | 2 (+ MIG serving in 5) |
| DRA-backed allocations honored | 3 (or documented blocker) |
| Results within expected performance envelope | 4–5 |

### Risks

| Risk | Mitigation |
|---|---|
| Mixed GPU-operator topology (sandbox + standard) | dedicate tray-11; second operator instance scoped by selector; verify tray-3/12 untouched after every change |
| DRA not GA on 4.22 + GPU Operator 26.7.2 | fallback documented in Phase 3 |
| GB300 MIG geometry differs from Hopper | discover at runtime before partitioning |
| vLLM/RHOAI runtime compatibility with Blackwell (`sm_103`) | verify early with the smoke test; pin a known-good vLLM build |
| ResNet-50 envelope on GB300 unpublished | self-established baseline + scaling-efficiency target |

---

*Status: plan approved, execution not started. Next action: Phase 0 step 1
(inspect tray-11 GPU state from the bastion).*
