#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export ROOT_DIR

# shellcheck disable=SC1091
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

: "${KUBE_CONTEXT:=lab-default}"
: "${BASE_DOMAIN:=kbsonlong.com}"
: "${LB_POOL_START:=192.168.3.240}"
: "${LB_POOL_STOP:=192.168.3.249}"
: "${GITEA_ADMIN_USER:=along}"
: "${GITEA_ADMIN_EMAIL:=admin@example.invalid}"
: "${CILIUM_VERSION:=1.20.2}"
: "${GATEWAY_API_VERSION:=v1.5.0}"
: "${ARGOCD_CHART_VERSION:=10.10.3}"
: "${GITEA_CHART_VERSION:=12.7.0}"
: "${GITEA_VERSION:=1.27.0}"
: "${CILIUM_NAMESPACE:=kube-system}"
: "${ARGOCD_NAMESPACE:=argocd}"
: "${GITEA_NAMESPACE:=gitea}"
: "${GATEWAY_NAMESPACE:=cilium-gateway}"

export KUBE_CONTEXT BASE_DOMAIN LB_POOL_START LB_POOL_STOP GITEA_ADMIN_USER GITEA_ADMIN_EMAIL
export GITEA_ADMIN_PASSWORD GITEA_DB_PASSWORD
export CILIUM_VERSION GATEWAY_API_VERSION ARGOCD_CHART_VERSION GITEA_CHART_VERSION GITEA_VERSION
export CILIUM_NAMESPACE ARGOCD_NAMESPACE GITEA_NAMESPACE GATEWAY_NAMESPACE

log() { printf '[homelab] %s\n' "$*"; }
die() { printf '[homelab][ERROR] %s\n' "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

kubectl_lab() { kubectl --context "$KUBE_CONTEXT" "$@"; }
helm_lab() { helm --kube-context "$KUBE_CONTEXT" "$@"; }

require_secret() {
  [[ -n "${GITEA_ADMIN_PASSWORD:-}" ]] || die 'GITEA_ADMIN_PASSWORD is empty; copy .env.example to .env and set it'
  [[ -n "${GITEA_DB_PASSWORD:-}" ]] || die 'GITEA_DB_PASSWORD is empty; copy .env.example to .env and set it'
}

wait_rollout() {
  local kind=$1 name=$2 namespace=$3 timeout=${4:-300s}
  kubectl_lab -n "$namespace" rollout status "$kind/$name" --timeout="$timeout"
}
