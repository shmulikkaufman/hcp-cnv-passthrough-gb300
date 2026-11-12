# HCP + OpenShift Virtualization + GB300 GPU & CX-8 RDMA Passthrough

End-to-end, validated reference for running KubeVirt virtual machines with
**2× GB300 GPUs and 4× ConnectX-8 VFs each** on a hosted (HCP) OpenShift
cluster on ARM64 (GB300 NVL72), including **VM-to-VM RoCE RDMA and
GPUDirect (GPU-memory ↔ GPU-memory) transfers at full line rate**.

> **Start here → [`Final/`](Final/README.md)** — the consolidated build
> guide. `Final/configs/` holds only the final, working configurations;
> Appendix A records every issue hit and Appendix B indexes every custom
> component with the reason it exists.

## Validated end state (2026-11-05)

| Layer | Component |
|---|---|
| Hub | OpenShift 5.0.0-rc.6, x86_64 (`hub-node-1`, 172.16.2.132) |
| Hosted cluster | OpenShift 4.22.19 HCP, Agent platform, arm64 multi-arch payload |
| Workers | `compute-tray-3`, `compute-tray-12` active; `compute-tray-11` Ready (its VM Halted) |
| Virtualization | CNV 4.22.12 **+ AIE** (NVIDIA-tuned virt-launcher, iommufd device plugin) |
| GPU operator | NVIDIA GPU Operator 26.7.2, **vm-passthrough** (sandbox) mode |
| VMs | `gpu-vm-tray-3`, `gpu-vm-tray-12`: RHEL 10.2 arm64 (64 KiB pages), 4 vCPUs, 2× GB300 + 4× CX-8 VF each |
| Guest GPU stack | NVIDIA open kernel modules 615.71.10 + CUDA 13.4/13.5, `sm_103` |

### Headline results (perftest `ib_write_bw`, 4 rails, jumbo MTU)

| Test | Result |
|---|---|
| 4-rail concurrent one-way | **1,560.84 Gb/s** — full line rate on every rail (390.08–390.28 each) |
| 4-rail concurrent bidirectional | **~2,656 Gb/s** aggregate |
| GPUDirect 4-rail (dma-buf) | **1,560.63 Gb/s** — full line rate, byte-exact content verified |

A topology diagram of the VMs, the four wires and their bandwidths is in
[`rdma-rails-diagram.html`](rdma-rails-diagram.html).

## Repository layout

| Path | Contents |
|---|---|
| [`Final/`](Final/README.md) | the consolidated build guide (10 chapters + appendices A/B) |
| [`Final/configs/`](Final/configs/) | **only the final, working configurations** |
| [`archive/`](archive/README.md) | superseded/historical material — never needed to rebuild |
| `scripts/` | convenience environment script |
| `rdma-gb300.html` | external reference document (gitignored, not for publication) |

BMC credentials, RHSM activation keys, kubeconfigs and cloud-init Secrets
are never stored in this repository.
