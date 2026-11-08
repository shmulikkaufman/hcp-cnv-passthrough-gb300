#!/usr/bin/env bash
#
# rdma-vm2vm-test.sh — VM-to-VM RDMA validation
# Cluster: GB300 HCP OpenShift 4.22 (gpu-vm-tray-11 / gpu-vm-tray-12)
# Path under test: BlueField-3 ConnectX-7 VFs (PCI passthrough) over RoCE
#
# Usage:
#   On the SERVER VM:      sudo ./rdma-vm2vm-test.sh server
#   On the CLIENT VM:      sudo ./rdma-vm2vm-test.sh <server-IPv4>
#
# Tests (client side):
#   1. write bandwidth, 1 QP, 5000 x 64 KiB
#   2. write bandwidth, 16 QPs, 10 s
#   3. bidirectional write bandwidth
#   4. write latency, 2-byte messages
#   plus RDMA hardware-counter proof that traffic used the VF (mlx5_0),
#   never the virtio management interface.
#
# Exit code 0 = all tests passed.
#
# Requirements: rdma-core, libibverbs-utils, perftest (RHEL 10 BaseOS).
# The VF interface must have an IPv4 address (same subnet on both VMs):
#   sudo nmcli device set enp10s0 managed no
#   sudo ip addr add 192.0.2.11/24 dev enp10s0        # (peer: .12)
#
# Notes:
#  - The RoCEv2 GID index is discovered automatically from the local IP.
#  - Uses perftest's default TCP exchange for QP info (the -R/rdma_cm mode
#    fails with rdma_resolve_addr on this stack). The control traffic also
#    stays on the VF: the test subnet only exists on enp10s0.

set -uo pipefail

DEV="${RDMA_DEV:-mlx5_0}"
PORT_BW1=18515
PORT_BW16=18516
PORT_BIDI=18517
PORT_LAT=18518
BW_PASS_GB=10          # minimum Gb/s that proves the RDMA (not virtio) path
RESULT=0

say()  { printf '>>> %s\n' "$*"; }
hdr()  { printf '\n============ %s ============\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# Echo the index of the RoCEv2 GID derived from IPv4 $2 on device $1.
find_gid_index() {
    local a b c d hex gid idx type
    IFS=. read -r a b c d <<< "$2"
    hex=$(printf '%02x%02x:%02x%02x' "$a" "$b" "$c" "$d")
    for g in /sys/class/infiniband/"$1"/ports/1/gids/*; do
        idx=${g##*/}
        gid=$(cat "$g" 2>/dev/null) || continue
        type=$(cat "/sys/class/infiniband/$1/ports/1/gid_attrs/types/$idx" 2>/dev/null)
        if [ "$type" = "RoCE v2" ] && [ "${gid#*$hex}" != "$gid" ]; then
            echo "$idx"; return 0
        fi
    done
    return 1
}

preflight() {
    command -v ib_write_bw  >/dev/null 2>&1 || die "perftest missing: sudo dnf install -y perftest"
    command -v ibv_devinfo  >/dev/null 2>&1 || die "rdma-core missing: sudo dnf install -y rdma-core libibverbs-utils"

    local link cidr
    link=$(rdma link show "$DEV/1" 2>/dev/null) || die "RDMA device $DEV not found"
    echo "$link" | grep -q "ACTIVE"      || die "$DEV/1 is not ACTIVE: $link"
    NETDEV=$(echo "$link" | sed -n 's/.*netdev \([^ ]*\).*/\1/p')
    [ -n "${NETDEV:-}" ]                 || die "no netdev bound to $DEV"

    cidr=$(ip -4 addr show dev "$NETDEV" | awk '/inet /{print $2; exit}')
    [ -n "${cidr:-}" ] || die "$NETDEV has no IPv4. Assign one first:
        sudo nmcli device set $NETDEV managed no
        sudo ip addr add <ip>/24 dev $NETDEV"
    MYIP=${cidr%%/*}

    GID=$(find_gid_index "$DEV" "$MYIP") || die "no RoCEv2 GID matching $MYIP on $DEV"
    say "local: dev=$DEV netdev=$NETDEV ip=$MYIP gid_index=$GID"
    say "gid:   $(cat /sys/class/infiniband/$DEV/ports/1/gids/$GID) (RoCE v2)"
}

counters() { rdma statistic show link "$DEV/1" 2>/dev/null; }

# Run one client-side bandwidth test; args: label port extra-perftest-args...
run_bw() {
    local label=$1 port=$2; shift 2
    hdr "$label"
    local out bw
    if ! out=$(timeout 120 ib_write_bw -d "$DEV" -x "$GID" -F --report_gbits \
                    -p "$port" "$@" "$PEER" 2>&1); then
        echo "$out" | tail -5
        say "TEST FAILED — is the server running on $PEER? ('$0 server')"
        RESULT=1; return
    fi
    echo "$out" | grep -E 'Mtu|GID index|Link type|#bytes|BW average' || echo "$out" | tail -3
    bw=$(echo "$out" | awk '/#bytes/{getline; print $4; exit}')
    if [ -n "${bw:-}" ] && awk -v b="$bw" -v m="$BW_PASS_GB" 'BEGIN{exit !(b>m)}'; then
        say "PASS: ${bw} Gb/s average (> ${BW_PASS_GB} Gb/s ⇒ RDMA path, not virtio)"
    else
        say "SUSPECT: bandwidth ${bw:-n/a} Gb/s — investigate"
        RESULT=1
    fi
}

server() {
    preflight
    say "starting 4 listeners (one per client test) — Ctrl-C to abort"
    local logdir=/tmp/rdma-vm2vm-server; mkdir -p "$logdir"; rm -f "$logdir"/*
    # NOTE: perftest pre-creates QPs at startup, so the 16-QP listener must
    # carry -q 16 on the server as well; message size / iters / bidirectional
    # are exchanged and adapted, so only -q needs mirroring.
    ib_write_bw  -d "$DEV" -x "$GID" -F --report_gbits -p $PORT_BW1  -s 65536 -n 5000 >"$logdir/bw1.log"  2>&1 &
    ib_write_bw  -d "$DEV" -x "$GID" -F --report_gbits -p $PORT_BW16 -q 16 -D 30        >"$logdir/bw16.log" 2>&1 &
    ib_write_bw  -d "$DEV" -x "$GID" -F --report_gbits -p $PORT_BIDI -s 65536 -n 5000 -b >"$logdir/bidi.log" 2>&1 &
    ib_write_lat -d "$DEV" -x "$GID" -F               -p $PORT_LAT  -s 2                 >"$logdir/lat.log"  2>&1 &
    wait
    hdr "server-side results"
    for f in bw1 bw16 bidi lat; do
        echo "--- $f ---"
        grep -E '#bytes|BW average|t_min|t_avg' "$logdir/$f.log" | head -3
    done
    say "all listeners served — server done"
}

client() {
    PEER=${1:-}
    [ -n "$PEER" ] || die "usage: $0 <server-IPv4>   (run '$0 server' on the peer VM first)"
    preflight
    ping -c 2 -W 2 "$PEER" >/dev/null 2>&1 || die "peer $PEER unreachable (L2/fabric or wrong IP)"
    say "peer $PEER reachable (L2 ok)"

    local before after latout lat
    before=$(counters)

    run_bw "TEST 1: write BW — 1 QP, 5000 x 64 KiB"      $PORT_BW1  -s 65536 -n 5000
    run_bw "TEST 2: write BW — 16 QPs, 10 s"             $PORT_BW16 -s 65536 -q 16 -D 10
    run_bw "TEST 3: bidirectional write BW — 1 QP"       $PORT_BIDI -s 65536 -n 5000 -b

    hdr "TEST 4: write latency — 2 B, 1000 iters"
    if latout=$(timeout 120 ib_write_lat -d "$DEV" -x "$GID" -F -p $PORT_LAT -s 2 "$PEER" 2>&1); then
        echo "$latout" | grep -E '#bytes|GID:|t_' | head -6
        lat=$(echo "$latout" | awk '/#bytes/{getline; print $6; exit}')
        say "PASS: average write latency ${lat:-n/a} µs"
    else
        echo "$latout" | tail -5; say "latency test failed"; RESULT=1
    fi

    hdr "ISOLATION PROOF — RDMA hardware counters (mlx5_0)"
    after=$(counters)
    local delta
    delta=$(diff <(echo "$before") <(echo "$after") | grep '^>' | sed 's/^> *//') || true
    if [ -n "$delta" ]; then
        echo "$delta"
        say "PASS: RDMA HW counters advanced — traffic traversed the VF, not virtio"
    else
        say "WARNING: no RDMA counter change detected"
        RESULT=1
    fi

    hdr "SUMMARY"
    if [ "$RESULT" -eq 0 ]; then
        say "ALL TESTS PASSED — VM-to-VM RDMA over the BlueField-3 VFs is working"
    else
        say "ONE OR MORE TESTS FAILED — see output above"
    fi
    exit "$RESULT"
}

case "${1:-}" in
    server) server ;;
    *)      client "${1:-}" ;;
esac
