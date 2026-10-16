#!/usr/bin/env bash
# Create only the isolated skillpulse k3d cluster. Never change the default kube context.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in docker k3d kubectl terraform helm openssl curl jq npx; do
  command -v "$tool" >/dev/null || { echo "Required CLI missing: $tool" >&2; exit 1; }
done
[[ -f .env ]] || { echo 'Copy .env.example to .env first.' >&2; exit 1; }
if [[ -f infra/terraform.tfstate ]] && ! k3d cluster list --no-headers | grep -q '^skillpulse '; then
  echo 'Local Terraform state exists but its cluster is absent. Run make down before recreating.' >&2
  exit 1
fi
if ! k3d registry list --no-headers | grep -q '^k3d-skillpulse-registry '; then
  k3d registry create skillpulse-registry --port '127.0.0.1:5001'
fi
if ! k3d cluster list --no-headers | grep -q '^skillpulse '; then
  k3d cluster create skillpulse --image rancher/k3s:v1.32.3-k3s1 \
    --registry-use k3d-skillpulse-registry:5000 --wait \
    --kubeconfig-update-default=false --kubeconfig-switch-context=false
fi
umask 077
k3d kubeconfig get skillpulse > infra/.kubeconfig
export KUBECONFIG="$PWD/infra/.kubeconfig"
export TF_VAR_kube_config_path="$KUBECONFIG"
# Native Windows CLIs require a native path, not Git Bash's /c/ path.
if command -v cygpath >/dev/null; then
  export KUBECONFIG="$(cygpath -m "$KUBECONFIG")"
  export TF_VAR_kube_config_path="$KUBECONFIG"
fi
export TF_VAR_kube_context=k3d-skillpulse
export TF_VAR_image_tag="${IMAGE_TAG:-dev-$(date +%s)}"
export TF_VAR_values_file="${VALUES_FILE:-values-local.yaml}"
docker build --tag "localhost:5001/skillpulse:$TF_VAR_image_tag" .
docker push "localhost:5001/skillpulse:$TF_VAR_image_tag"
terraform -chdir=infra init -input=false
# The CRD must exist before kubernetes_manifest can plan the database resource.
terraform -chdir=infra apply -input=false -auto-approve -target=helm_release.cnpg
kubectl --context k3d-skillpulse wait --for=condition=Established crd/clusters.postgresql.cnpg.io --timeout=120s
bash scripts/secrets.sh
terraform -chdir=infra apply -input=false -auto-approve -target=kubernetes_manifest.cnpg_cluster \
  -target=kubernetes_deployment.redis -target=kubernetes_service.redis
kubectl --context k3d-skillpulse -n skillpulse wait --for=condition=Ready cluster/skillpulse-db --timeout=300s
terraform -chdir=infra apply -input=false -auto-approve
# Migration creates the NOLOGIN privilege role; CNPG manages the separate LOGIN roles.
# Grant membership only after both exist. This also upgrades existing local clusters.
pod=$(kubectl --context k3d-skillpulse -n skillpulse get pods -l cnpg.io/cluster=skillpulse-db,role=primary -o jsonpath='{.items[0].metadata.name}')
kubectl --context k3d-skillpulse -n skillpulse exec -i "$pod" -c postgres -- \
  psql -U postgres -d skillpulse -v ON_ERROR_STOP=1 < scripts/db-readers.sql
bash scripts/smoke.sh
