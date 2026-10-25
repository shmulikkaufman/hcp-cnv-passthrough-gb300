#!/usr/bin/env bash
# In-guest NVIDIA driver install for RHEL 10 (ARM64) with a passed-through GB300.
# Run inside the VM as root. Requires the NVIDIA driver RPM repo (rhel-10-for-
# aarch64-nvidia-rpms) and the CUDA repo (cuda-rhel10-sbsa).
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
