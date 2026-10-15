#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"

for cmd in kubectl helm curl python3 git; do need_cmd "$cmd"; done
[[ -f "$ROOT_DIR/.env" ]] || log 'warning: .env not found; plan/check can continue, deploy will require secrets'

actual=$(kubectl config current-context 2>/dev/null || true)
[[ "$actual" == "$KUBE_CONTEXT" ]] || die "kubectl context is '$actual', expected '$KUBE_CONTEXT'"
kubectl_lab cluster-info >/dev/null
a=$(kubectl_lab get nodes --no-headers 2>/dev/null | awk '$2 == "Ready" {count++} END {print count+0}')
(( a >= 1 )) || die 'no Ready Kubernetes node found'
helm version >/dev/null

printf 'OK context=%s ready_nodes=%s cilium=%s gateway_api=%s\n' "$KUBE_CONTEXT" "$a" "$CILIUM_VERSION" "$GATEWAY_API_VERSION"
