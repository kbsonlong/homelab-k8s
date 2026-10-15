#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"
[[ "${CONFIRM:-}" == destroy ]] || die 'destructive operation blocked; run CONFIRM=destroy make destroy'

cat <<'WARN'
This removes application namespaces and project Gateway resources from the selected cluster.
It does not delete nodes, Colima, PVE VMs, or the Cilium Helm release.
PVCs may contain lab data and are not deleted automatically by this script.
WARN
read -r -p 'Type DESTROY to continue: ' answer
[[ "$answer" == DESTROY ]] || die 'confirmation did not match'

kubectl_lab delete -f "$ROOT_DIR/rendered/gateway-api.yaml" --ignore-not-found
kubectl_lab delete -f "$ROOT_DIR/rendered/cilium.yaml" --ignore-not-found
helm_lab uninstall gitea -n "$GITEA_NAMESPACE" 2>/dev/null || true
helm_lab uninstall argo-cd -n "$ARGOCD_NAMESPACE" 2>/dev/null || true
kubectl_lab delete namespace "$GATEWAY_NAMESPACE" "$GITEA_NAMESPACE" "$ARGOCD_NAMESPACE" --ignore-not-found
printf 'Destroy completed; inspect PVCs and cluster state manually.\n'
