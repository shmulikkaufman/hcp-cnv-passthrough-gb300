#!/bin/sh
# KubeVirt sidecar hook (onDefineDomain) that enlarges the guest's 64-bit PCI MMIO
# window so the GB300's 512 GiB BAR fits.
#
# WHY: the QEMU `virt` machine hard-codes the above-4G MMIO window to 512 GiB
# (DEFAULT_HIGH_PCIE_MMIO_SIZE_GB in hw/arm/virt.c). The GB300 exposes ~520 GiB of
# BARs, so guest firmware and kernel cannot assign them:
#   "pci 0000:0a:00.0: BAR 2 [mem size 0x8000000000 64bit pref]: can't assign; no space"
#   "NVRM: BAR0 is 0M @ 0x0" -> nvidia probe fails.
#
# IMPORTANT (QEMU >= 9.x/10.x): the often-documented
#   -global gpex-pcihost.x-above-4g-mmio-base=... / x-above-4g-mmio-size=16T
# does NOT work — the properties are accepted (visible in `info qtree`) but the
# guest-visible pcie-mmio-high alias is built from the machine memmap regardless.
# The correct knob is the MACHINE property:
#   -machine highmem-mmio-size=16T
# A second `-machine` flag on the QEMU command line merges with libvirt's own
# `-machine virt-rhel10.2.0`, so it can be injected via qemu:commandline.
#
# Resulting window: 16 TiB - 32 TiB (guest has host-passthrough Grace CPU with
# 48-bit PA, so this fits). 1 TiB would also be sufficient for ~520 GiB of BARs.
#
# Deploy as a ConfigMap next to the VM:
#   oc -n gpu-vm-test create configmap gpu-mmio-hook \
#     --from-file=onDefineDomain.sh=gpu-mmio-hook.sh
# and reference it from the VM template annotation:
#   hooks.kubevirt.io/hookSidecars: '[{"args":["--version","v1alpha2"],
#     "configMap":{"name":"gpu-mmio-hook","key":"onDefineDomain.sh",
#     "hookPath":"/usr/bin/onDefineDomain"}}]'
# Takes effect on the next VM start (stop/start).
set -eu
domain=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain)
      domain="$2"
      shift 2
      ;;
    --vmi)
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
[ -n "$domain" ] || { echo "missing --domain argument" >&2; exit 1; }
if ! printf '%s\n' "$domain" | grep -q 'xmlns:qemu='; then
  domain=$(printf '%s\n' "$domain" | sed 's|<domain |<domain xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0" |')
fi
if ! printf '%s\n' "$domain" | grep -q '<qemu:commandline>'; then
  domain=$(printf '%s\n' "$domain" | sed 's|</domain>|<qemu:commandline><qemu:arg value="-machine"/><qemu:arg value="highmem-mmio-size=16T"/></qemu:commandline></domain>|')
fi
printf '%s\n' "$domain"
