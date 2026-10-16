#!/usr/bin/env bash
# Bootstrap an isolated local/CI cluster. Never source .env as executable shell code.
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-$PWD/infra/.kubeconfig}"
context="${KUBE_CONTEXT:-k3d-skillpulse}"
namespace=skillpulse
if [[ "$context" != k3d-skillpulse ]]; then
  echo 'Refusing secret bootstrap outside the k3d-skillpulse context.' >&2
  exit 1
fi
[[ -f .env ]] || { echo 'Copy .env.example to .env first.' >&2; exit 1; }
command -v openssl >/dev/null
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kube=(kubectl --context "$context" --namespace "$namespace")
if "${kube[@]}" get secret skillpulse-db-owner >/dev/null 2>&1; then
  password=$("${kube[@]}" get secret skillpulse-db-owner -o jsonpath='{.data.password}' | base64 --decode)
else
  password=$(openssl rand -hex 24)
fi
printf 'username=skillpulse\npassword=%s\nDATABASE_URL=postgresql+psycopg2://skillpulse:%s@skillpulse-db-rw:5432/skillpulse\n' "$password" "$password" > "$work/owner.env"
"${kube[@]}" create secret generic skillpulse-db-owner --type=kubernetes.io/basic-auth \
  --from-env-file="$work/owner.env" --dry-run=client -o yaml | "${kube[@]}" apply --server-side -f - >/dev/null
# Keep user configuration, but never point the cluster at the host database or enable live ingestion.
grep -v -E '^(SKILLPULSE_DATABASE_URL|SKILLPULSE_REDIS_URL|SKILLPULSE_HN_LIVE_FETCH_ENABLED|SKILLPULSE_AGENT_LLM_ENABLED|SKILLPULSE_LLM_ENABLED|SKILLPULSE_LANGFUSE_ENABLED)=' .env > "$work/app.env" || [[ $? == 1 ]]
printf '\nSKILLPULSE_DATABASE_URL=postgresql+psycopg2://skillpulse:%s@skillpulse-db-rw:5432/skillpulse\n' "$password" >> "$work/app.env"
printf 'SKILLPULSE_REDIS_URL=redis://skillpulse-redis:6379/0\nSKILLPULSE_HN_LIVE_FETCH_ENABLED=false\nSKILLPULSE_AGENT_LLM_ENABLED=false\nSKILLPULSE_LLM_ENABLED=false\nSKILLPULSE_LANGFUSE_ENABLED=false\n' >> "$work/app.env"
"${kube[@]}" create secret generic skillpulse --from-env-file="$work/app.env" \
  --dry-run=client -o yaml | "${kube[@]}" apply --server-side -f - >/dev/null
# API/MCP get separate configuration Secrets: neither contains the owner's DSN.
for component in api mcp; do
  role="skillpulse_$component"
  secret="skillpulse-db-$component"
  if "${kube[@]}" get secret "$secret" >/dev/null 2>&1; then
    reader_password=$("${kube[@]}" get secret "$secret" -o jsonpath='{.data.password}' | base64 --decode)
  else
    reader_password=$(openssl rand -hex 24)
  fi
  printf 'username=%s\npassword=%s\n' "$role" "$reader_password" > "$work/reader.env"
  "${kube[@]}" create secret generic "$secret" --type=kubernetes.io/basic-auth \
    --from-env-file="$work/reader.env" --dry-run=client -o yaml | "${kube[@]}" apply --server-side -f - >/dev/null
  grep -v '^SKILLPULSE_DATABASE_URL=' "$work/app.env" > "$work/$component.env"
  printf '\nSKILLPULSE_DATABASE_URL=postgresql+psycopg2://%s:%s@skillpulse-db-rw:5432/skillpulse\n' "$role" "$reader_password" >> "$work/$component.env"
  "${kube[@]}" create secret generic "skillpulse-$component" --from-env-file="$work/$component.env" \
    --dry-run=client -o yaml | "${kube[@]}" apply --server-side -f - >/dev/null
  unset reader_password
 done
echo 'Local Kubernetes secrets are ready. API/MCP use separate credentials; external gates remain disabled.'
