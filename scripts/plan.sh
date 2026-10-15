#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"

"$ROOT_DIR/scripts/check.sh"
helm_lab repo add cilium https://helm.cilium.io/ >/dev/null 2>&1 || true
helm_lab repo update cilium >/dev/null
helm_lab repo add argo https://argoproj.github.io/argo-helm/ >/dev/null 2>&1 || true
helm_lab repo update argo >/dev/null
mkdir -p "$ROOT_DIR/rendered"
"$ROOT_DIR/scripts/render.sh"

kubectl_lab apply --server-side --force-conflicts --dry-run=server -f "$ROOT_DIR/rendered/cilium.yaml"
kubectl_lab apply --server-side --force-conflicts --dry-run=server -f "$ROOT_DIR/rendered/gateway-api.yaml"

helm_lab template argo-cd argo/argo-cd --version "$ARGOCD_CHART_VERSION" -n "$ARGOCD_NAMESPACE" -f "$ROOT_DIR/rendered/argocd-values.yaml" >/dev/null
helm_lab template gitea oci://docker.gitea.com/charts/gitea --version "$GITEA_CHART_VERSION" -n "$GITEA_NAMESPACE" -f "$ROOT_DIR/rendered/gitea-values.yaml" \
  --set gitea.admin.existingSecret=gitea-admin-auth \
  --set postgresql.global.postgresql.auth.existingSecret=gitea-postgresql-auth \
  --set-string gitea.admin.email="${GITEA_ADMIN_EMAIL:-admin@example.invalid}" >/dev/null
printf 'PLAN OK: no cluster state changed\n'
