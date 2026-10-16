#!/usr/bin/env bash
# Explicitly destructive: removes this project's disposable cluster and local state.
set -euo pipefail
cd "$(dirname "$0")/.."
k3d cluster delete skillpulse
if k3d registry list --no-headers | grep -q '^k3d-skillpulse-registry '; then
  k3d registry delete skillpulse-registry
fi
rm -f infra/.kubeconfig infra/terraform.tfstate infra/terraform.tfstate.backup
echo 'Removed the isolated skillpulse cluster and its local Terraform state.'
