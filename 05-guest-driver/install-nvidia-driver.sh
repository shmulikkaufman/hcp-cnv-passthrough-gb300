#!/usr/bin/env bash
# In-guest NVIDIA driver install for RHEL 10 (ARM64) with a passed-through GB300.
# Run inside the VM as root.
#
# Repo prerequisites (see Phase 5 of the top-level README):
#   0. subscription-manager registered, rhel-10-for-aarch64-nvidia-rpms enabled
#   1. CRB + EPEL enabled (dkms, egl-* deps):
#        sudo subscription-manager repos --enable=codeready-builder-for-rhel-10-aarch64-rpms
#        sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm
#   2. Public CUDA repo:
#        sudo dnf config-manager --add-repo \
#          https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/cuda-rhel10.repo
#
# NOTE: kmod and userspace versions MUST match exactly, and nvidia-smi ships in
# nvidia-driver-cuda, NOT in nvidia-driver / kmod-nvidia-open-dkms.
#
# ⚠️ Do NOT install nvidia-forcebarpath.conf on CNV 4.22 + AIE — RMForceBarPath=1
# was a CNV 4.21 workaround and now breaks GPU init (kbusVerifyBar2_GH100
# MMUTest BAR0 failure). The 4.22 NV launcher provides the coherent-memory
# path natively. The conf file is kept in this directory for 4.21 stacks only.
set -euo pipefail

DRIVER_VERSION=615.71.10
KERNEL_VERSION=$(uname -r | sed 's/+64k//')   # pin devel to the running kernel

# Kernel devel packages matching the running el10nv kernel (else dnf pulls a
# newer kernel-core and DKMS builds for the wrong kernel)
dnf install -y kernel-64k-devel-${KERNEL_VERSION} \
               kernel-devel-matched-${KERNEL_VERSION}

# Kernel module + base userspace (from the RHEL NVIDIA repo)
dnf install -y kmod-nvidia-open-dkms-${DRIVER_VERSION} \
               nvidia-driver-${DRIVER_VERSION} \
               nvidia-modprobe-${DRIVER_VERSION}

# nvidia-smi + CUDA integration (from the CUDA repo)
dnf install -y nvidia-driver-cuda-${DRIVER_VERSION}

echo "Done. Reboot the VM to load the driver (no modprobe.d overrides needed on CNV 4.22)."
