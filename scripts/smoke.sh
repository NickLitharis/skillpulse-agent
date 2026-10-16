#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-$PWD/infra/.kubeconfig}"
context="${KUBE_CONTEXT:-k3d-skillpulse}"
[[ "$context" == k3d-skillpulse ]] || { echo 'Smoke fixture is restricted to k3d-skillpulse.' >&2; exit 1; }
for tool in kubectl curl jq npx; do
  command -v "$tool" >/dev/null || { echo "Required CLI missing: $tool" >&2; exit 1; }
done
kube=(kubectl --context "$context" --namespace skillpulse)
for component in api mcp worker-fetch worker-skills beat; do
  "${kube[@]}" rollout status "deployment/skillpulse-$component" --timeout=300s
done
pod=$("${kube[@]}" get pods -l cnpg.io/cluster=skillpulse-db,role=primary -o jsonpath='{.items[0].metadata.name}')
taxonomy=$(awk '/^version:/ { gsub(/["\r]/, "", $2); print $2; exit }' config/skills.yaml)
[[ -n "$pod" && -n "$taxonomy" ]]
"${kube[@]}" exec -i "$pod" -c postgres -- psql -U postgres -d skillpulse \
  -v "taxonomy_version=$taxonomy" < scripts/smoke-fixture.sql
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Connect with each actual LOGIN over TCP. Session READ ONLY is deliberately disabled:
# SQLSTATE 42501 must come from grants, not a failed connection or transaction guard.
for component in api mcp; do
  role="skillpulse_$component"
  reader_password=$("${kube[@]}" get secret "skillpulse-db-$component" -o jsonpath='{.data.password}' | base64 --decode)
  run_reader() {
    { printf '%s\n' "$reader_password"; printf '\\set VERBOSITY sqlstate\nSET default_transaction_read_only = off;\n%s\n' "$1"; } |
      "${kube[@]}" exec -i "$pod" -c postgres -- sh -c \
        'read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U "$1" -d skillpulse -At -v ON_ERROR_STOP=1' sh "$role"
  }
  run_reader "SELECT current_user || ':' || count(*) FROM posts WHERE id = -900002 GROUP BY current_user;" > "$work/reader.log" 2>&1
  grep -Fxq "$role:1" "$work/reader.log"
  run_reader 'SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls) FROM pg_roles WHERE rolname = current_user;' > "$work/reader.log" 2>&1
  grep -Fxq 't' "$work/reader.log"
  for statement in 'INSERT INTO posts (id) VALUES (-900099)' 'UPDATE posts SET is_dead = false WHERE false' 'CREATE TABLE public.__skillpulse_permission_probe (id integer)'; do
    if run_reader "BEGIN READ WRITE; $statement; ROLLBACK;" > "$work/reader.log" 2>&1; then
      echo "Security check failed: $role could write." >&2; exit 1
    fi
    grep -q 'ERROR:  *42501' "$work/reader.log" || { echo "Security check failed: $role did not return permission-denied SQLSTATE." >&2; exit 1; }
  done
  unset reader_password
 done
echo 'Security smoke passed: both reader logins SELECT successfully; INSERT, UPDATE and DDL denied by privileges.'
api_port="${SMOKE_API_PORT:-18000}"
mcp_port="${SMOKE_MCP_PORT:-18765}"
"${kube[@]}" port-forward service/skillpulse-api "$api_port:8000" > "$work/api.log" 2>&1 &
api_pid=$!
"${kube[@]}" port-forward service/skillpulse-mcp "$mcp_port:8765" > "$work/mcp.log" 2>&1 &
mcp_pid=$!
cleanup() {
  kill "$api_pid" "$mcp_pid" 2>/dev/null || true
  wait "$api_pid" "$mcp_pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
ready=false
for _ in {1..40}; do
  if curl --silent --fail "http://127.0.0.1:$api_port/readyz" > "$work/ready.json"; then ready=true; break; fi
  sleep 1
done
[[ "$ready" == true ]] || { echo 'Readiness failed.' >&2; exit 1; }
jq -e '.status == "ready"' "$work/ready.json" >/dev/null
curl --silent --fail "http://127.0.0.1:$api_port/metrics" > "$work/api-metrics.txt"
grep -q 'skillpulse_requests_total' "$work/api-metrics.txt"
grep -q '^skillpulse_extraction_scrape_ok 1.0$' "$work/api-metrics.txt"
grep -q '^skillpulse_extraction_records{extractor="baseline"} 1.0$' "$work/api-metrics.txt"
curl --silent --fail "http://127.0.0.1:$mcp_port/metrics" > "$work/mcp-before.txt"
before=$(awk '/^skillpulse_mcp_tool_calls_total\{outcome="ok",tool="skill_counts"\}/ {print $2}' "$work/mcp-before.txt")
npx --yes --package=@modelcontextprotocol/inspector@2.11.0 inspector --cli \
  --transport http --server-url "http://127.0.0.1:$mcp_port/mcp" \
  --method tools/call --tool-name skill_counts --tool-arg top_n=1 > "$work/mcp.json"
jq -e '.isError == false and .structuredContent.tool == "skill_counts" and .structuredContent.row_count == 1 and .structuredContent.rows[0].skill_id == "python"' "$work/mcp.json" >/dev/null
curl --silent --fail "http://127.0.0.1:$mcp_port/metrics" > "$work/mcp-metrics.txt"
after=$(awk '/^skillpulse_mcp_tool_calls_total\{outcome="ok",tool="skill_counts"\}/ {print $2}' "$work/mcp-metrics.txt")
awk -v before="${before:-0}" -v after="${after:-0}" 'BEGIN {exit !(after == before + 1)}'
echo 'Smoke passed: readiness, DB-derived extraction metrics, MCP call metrics and synthetic skill_counts.'
