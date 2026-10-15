#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"

"$ROOT_DIR/scripts/check.sh"
require_secret
"$ROOT_DIR/scripts/render.sh"

log "install Gateway API CRDs $GATEWAY_API_VERSION"
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  kubectl_lab apply --server-side --force-conflicts -f "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/$GATEWAY_API_VERSION/config/crd/experimental/$file"
done < "$ROOT_DIR/config/gateway/gateway-api-crds.txt"

log "upgrade Cilium $CILIUM_VERSION"
helm_lab repo add cilium https://helm.cilium.io/ >/dev/null 2>&1 || true
helm_lab repo update cilium >/dev/null
helm_lab upgrade --install cilium cilium/cilium --version "$CILIUM_VERSION" -n "$CILIUM_NAMESPACE" --reuse-values \
  --set gatewayAPI.enabled=true \
  --set l2announcements.enabled=true
kubectl_lab -n "$CILIUM_NAMESPACE" rollout restart ds/cilium
kubectl_lab -n "$CILIUM_NAMESPACE" rollout status ds/cilium --timeout=300s
kubectl_lab -n "$CILIUM_NAMESPACE" rollout restart deploy/cilium-operator
kubectl_lab -n "$CILIUM_NAMESPACE" rollout status deploy/cilium-operator --timeout=300s
kubectl_lab -n "$CILIUM_NAMESPACE" rollout restart ds/cilium-envoy 2>/dev/null || true
kubectl_lab -n "$CILIUM_NAMESPACE" rollout status ds/cilium-envoy --timeout=300s 2>/dev/null || true
kubectl_lab apply --server-side --force-conflicts -f "$ROOT_DIR/rendered/cilium.yaml"

log 'install Argo CD with ClusterIP server'
kubectl_lab create namespace "$ARGOCD_NAMESPACE" --dry-run=client -o yaml | kubectl_lab apply -f -
helm_lab repo add argo https://argoproj.github.io/argo-helm/ >/dev/null 2>&1 || true
helm_lab repo update argo >/dev/null
helm_lab upgrade --install argo-cd argo/argo-cd --version "$ARGOCD_CHART_VERSION" -n "$ARGOCD_NAMESPACE" -f "$ROOT_DIR/rendered/argocd-values.yaml"
wait_rollout deployment argo-cd-argocd-server "$ARGOCD_NAMESPACE" 300s

log 'install Gitea with ClusterIP HTTP and LoadBalancer SSH'
kubectl_lab create namespace "$GITEA_NAMESPACE" --dry-run=client -o yaml | kubectl_lab apply -f -
python3 - <<'PY' | kubectl_lab apply -f -
import base64, os

def b64(value):
    return base64.b64encode(value.encode()).decode()

print(f'''apiVersion: v1
kind: Secret
metadata:
  name: gitea-admin-auth
  namespace: {os.environ["GITEA_NAMESPACE"]}
type: Opaque
data:
  username: {b64(os.environ["GITEA_ADMIN_USER"])}
  password: {b64(os.environ["GITEA_ADMIN_PASSWORD"])}
---
apiVersion: v1
kind: Secret
metadata:
  name: gitea-postgresql-auth
  namespace: {os.environ["GITEA_NAMESPACE"]}
type: Opaque
data:
  password: {b64(os.environ["GITEA_DB_PASSWORD"])}
  postgres-password: {b64(os.environ["GITEA_DB_PASSWORD"])}
''')
PY
helm_lab upgrade --install gitea oci://docker.gitea.com/charts/gitea --version "$GITEA_CHART_VERSION" -n "$GITEA_NAMESPACE" \
  --set-string gitea.admin.email="$GITEA_ADMIN_EMAIL" \
  -f "$ROOT_DIR/rendered/gitea-values.yaml"

log 'apply shared Gateway and HTTPRoutes'
kubectl_lab create namespace "$GATEWAY_NAMESPACE" --dry-run=client -o yaml | kubectl_lab apply -f -
kubectl_lab apply --server-side --force-conflicts -f "$ROOT_DIR/rendered/gateway-api.yaml"

"$ROOT_DIR/scripts/verify.sh"
