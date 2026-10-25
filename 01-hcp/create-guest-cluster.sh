#!/usr/bin/env bash
# Create the hosted (guest) control plane on the hub cluster using the Agent platform.
# Requires: `hcp` CLI (from hypershift release), hub kubeconfig, pull secret, SSH keypair.
set -euo pipefail
cd /home/nvidia/workspaces/skaufman/task_hcp
export KUBECONFIG="$PWD/kubeconfig_hub"

./hcp create cluster agent \
  --name my-hosted-cluster \
  --namespace clusters \
  --agent-namespace clusters \
  --arch arm64 \
  --release-image quay.io/openshift-release-dev/ocp-release:4.22.0-multi \
  --pull-secret "$PWD/pull-secret.json" \
  --ssh-key /home/nvidia/.ssh/hcp_rsa.pub \
  --base-domain 172-16-2-132.sslip.io \
  --api-server-address 172.16.2.132 \
  --network-type OVNKubernetes \
  --cluster-cidr 10.132.0.0/14 \
  --service-cidr 172.31.0.0/16 \
  --control-plane-availability-policy SingleReplica \
  --infra-availability-policy SingleReplica \
  --etcd-storage-class lvms-vg1 \
  --etcd-storage-size 8Gi \
  --node-pool-replicas 1 \
  --node-upgrade-type InPlace

# After the HostedCluster + InfraEnv are created, download the discovery ISO:
#   oc get infraenv my-hosted-cluster -n clusters -o jsonpath='{.status.isoDownloadURL}'
# Boot the ARM64 worker node (GB300 tray) from that ISO; it registers as an Agent and
# is bound to the NodePool. Then extract the guest kubeconfig:
#   oc get secret my-hosted-cluster-admin-kubeconfig -n clusters \
#     -o jsonpath='{.data.kubeconfig}' | base64 -d > guest-kubeconfig
