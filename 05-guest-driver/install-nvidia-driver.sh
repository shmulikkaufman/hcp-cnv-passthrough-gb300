#!/usr/bin/env bash
# In-guest NVIDIA driver install for RHEL 10 (ARM64) with a passed-through GB300.
# Run inside the VM as root.
#
# Repo prerequisites (the rhel4nv image ships these already; a stock RHEL image
# needs steps 1-2 first):
#   1. Public CUDA repo (provides nvidia-driver*, nvidia-driver-cuda, kmod):
#        sudo dnf config-manager --add-repo \
#          https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/cuda-rhel10.repo
#      (equivalently the cuda-rhel10.repo file:
#        [cuda-rhel10-sbsa]
#        name=cuda-rhel10-sbsa
#        baseurl=https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa
#        enabled=1
#        gpgcheck=1
#        gpgkey=https://developer.download.nvidia.com/compute/cuda/repos/rhel10/sbsa/CDF6BA43.pub)
#   2. The "rhel-10-for-aarch64-nvidia-rpms" driver channel (kmod-nvidia-open-dkms,
#      nvidia-modprobe, el10nv kernel bits) — attached via Red Hat subscription
#      (RHEL for NVIDIA entitlement, appears in /etc/yum.repos.d/redhat.repo).
#      Verify with: dnf repolist | grep nvidia
#
# NOTE: kmod and userspace versions MUST match exactly, and nvidia-smi ships in
# nvidia-driver-cuda, NOT in nvidia-driver / kmod-nvidia-open-dkms.
set -euo pipefail

DRIVER_VERSION=615.71.10

# Kernel module + base userspace (from the RHEL NVIDIA repo)
dnf install -y kmod-nvidia-open-dkms-${DRIVER_VERSION} \
               nvidia-driver-${DRIVER_VERSION} \
               nvidia-modprobe-${DRIVER_VERSION}

# nvidia-smi + CUDA integration (from the CUDA repo)
dnf install -y nvidia-driver-cuda-${DRIVER_VERSION}

# Force BAR path (see nvidia-forcebarpath.conf in this directory)
install -m 0644 "$(dirname "$0")/nvidia-forcebarpath.conf" \
  /etc/modprobe.d/nvidia-forcebarpath.conf

echo "Done. Reboot the VM for the driver to load with RMForceBarPath=1."
