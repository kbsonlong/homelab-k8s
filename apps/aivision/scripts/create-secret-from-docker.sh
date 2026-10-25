#!/usr/bin/env bash
# Create the runtime Secret from the currently verified Docker deployment.
# This script is local-only and must never be committed with its output.
set -euo pipefail

NS=${NS:-aivision}
KUBECTL=${KUBECTL:-kubectl}

get_env() {
  docker inspect "$1" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | awk -F= -v key="$2" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 1}'
}

ob_pw=$(get_env aivision-be DB_PASSWORD)
secret_key=$(get_env aivision-be SECRET_KEY)
ingest_secret=$(get_env aivision-be AIVISION_INGEST_SECRET)
ingest_key=$(get_env antcollector-gw SIGNOZ_INGEST_INTERNAL_KEY)
agentlens_token=$(get_env aivision-be AGENTLENS_TOKEN)
signoz_root_pw=$(get_env aivision-be SIGNOZ_USER_ROOT_PASSWORD)
org_id=$(docker exec aivision-be python manage.py signoz_integration_status --print-org | tail -1 | tr -d '\r')

$KUBECTL -n "$NS" create secret generic aivision-runtime \
  --from-literal=OB_TENANT_PASSWORD="$ob_pw" \
  --from-literal=SECRET_KEY="$secret_key" \
  --from-literal=AIVISION_INGEST_SECRET="$ingest_secret" \
  --from-literal=SIGNOZ_INGEST_INTERNAL_KEY="$ingest_key" \
  --from-literal=AGENTLENS_TOKEN="$agentlens_token" \
  --from-literal=SIGNOZ_USER_ROOT_PASSWORD="$signoz_root_pw" \
  --from-literal=SIGNOZ_OCEANBASE_DSN="root:${ob_pw}@tcp(seekdb:2881)/signoz?charset=utf8mb4" \
  --from-literal=AGENTLENS_OB_URL="mysql+pymysql://root:${ob_pw}@seekdb:2881/agentlens?charset=utf8mb4" \
  --from-literal=SIGNOZ_OB_URL="mysql+pymysql://root:${ob_pw}@seekdb:2881/signoz?charset=utf8mb4" \
  --from-literal=SIGNOZ_ORG_ID="$org_id" \
  --dry-run=client -o yaml | $KUBECTL apply -f -

$KUBECTL -n "$NS" label secret/aivision-runtime app.kubernetes.io/part-of=ai-vision --overwrite
