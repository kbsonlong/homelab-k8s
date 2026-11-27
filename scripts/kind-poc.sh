#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="$ROOT_DIR/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$ROOT_DIR/.env.example"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi
CLUSTER_NAME=${KIND_CLUSTER_NAME:-waf-poc}
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
KIND_KUBECONFIG=${KIND_KUBECONFIG:-/tmp/kind-${CLUSTER_NAME}.kubeconfig}
export HELM_CACHE_HOME=${HELM_CACHE_HOME:-/tmp/helm-cache-waf-poc}
export HELM_CONFIG_HOME=${HELM_CONFIG_HOME:-/tmp/helm-config-waf-poc}
export HELM_DATA_HOME=${HELM_DATA_HOME:-/tmp/helm-data-waf-poc}
VM_STACK_VERSION=${VM_STACK_VERSION:-0.98.1}
VM_VALUES=$(mktemp)
trap 'rm -f "$VM_VALUES"' EXIT

for cmd in kind kubectl helm docker curl sed; do
  command -v "$cmd" >/dev/null || { echo "missing command: $cmd" >&2; exit 2; }
done

if ! kind get clusters | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME" --kubeconfig "$KIND_KUBECONFIG" --config - <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
EOF
elif [[ ! -f "$KIND_KUBECONFIG" ]]; then
  kind export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$KIND_KUBECONFIG"
fi

kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" cluster-info >/dev/null

helm repo add vm https://victoriametrics.github.io/helm-charts/ >/dev/null 2>&1 || true
helm repo update vm >/dev/null

# kind ships a local-path provisioner and default StorageClass in its node image.
kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" -n local-path-storage rollout status deploy/local-path-provisioner --timeout=180s

# The production values pin storage workloads to the Colima node. Retarget that selector to this kind node.
sed -e "s/kubernetes.io\/hostname: colima/kubernetes.io\/hostname: ${CLUSTER_NAME}-control-plane/g" \
  -e 's/kubernetes.pod_namespace:=cilium-gateway or (/kubernetes.pod_namespace:=cilium-gateway or kubernetes.pod_namespace:=waf or (/' \
  "$ROOT_DIR/helm/victoriametrics/values.yaml" > "$VM_VALUES"
helm_args=(--version "$VM_STACK_VERSION" -n monitoring --create-namespace -f "$VM_VALUES" --wait --timeout 30m)
helm_args+=(--set grafana.livenessProbe.initialDelaySeconds=600)
if [[ -n "${HTTP_PROXY:-}" ]]; then
  helm_args+=(--set-string "grafana.env.HTTP_PROXY=${HTTP_PROXY}")
fi
if [[ -n "${HTTPS_PROXY:-}" ]]; then
  helm_args+=(--set-string "grafana.env.HTTPS_PROXY=${HTTPS_PROXY}")
fi
NO_PROXY_VALUE=${NO_PROXY:-localhost,127.0.0.1,.svc,.cluster.local,10.0.0.0/8,10.244.0.0/16,192.168.3.0/24}
NO_PROXY_VALUE=${NO_PROXY_VALUE//,/\\,}
helm_args+=(--set-string "grafana.env.NO_PROXY=${NO_PROXY_VALUE}")
helm upgrade --install vmk8s vm/victoria-metrics-k8s-stack --kubeconfig "$KIND_KUBECONFIG" --kube-context "$KUBE_CONTEXT" \
  "${helm_args[@]}"

kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" apply -f "$ROOT_DIR/config/waf/kind-poc.yaml"
kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" -n monitoring create configmap grafana-dashboard-waf \
  --from-file=waf.json="$ROOT_DIR/config/observability/waf/dashboards/waf.json" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" apply -f -
kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" -n monitoring label configmap grafana-dashboard-waf \
  grafana_dashboard=1 --overwrite
kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" -n monitoring annotate configmap grafana-dashboard-waf \
  grafana_folder=WAF --overwrite

kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" -n waf rollout status deploy/crs-grafana --timeout=300s
kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" -n monitoring rollout status deploy/vmk8s-grafana --timeout=600s

cat <<EOF
POC ready in context ${KUBE_CONTEXT}.
Kubeconfig: ${KIND_KUBECONFIG}
WAF-fronted Grafana: kubectl --kubeconfig ${KIND_KUBECONFIG} --context ${KUBE_CONTEXT} -n waf port-forward deploy/crs-grafana 8080:8080
Use http://127.0.0.1:8080 with Host: grafana.kbsonlong.com.
EOF
