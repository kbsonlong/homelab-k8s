#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"

"$ROOT_DIR/scripts/check.sh"
"$ROOT_DIR/scripts/render.sh"

printf '%s\n' '=== nodes ==='
kubectl_lab get nodes -o wide
printf '%s\n' '=== releases ==='
helm_lab list -A
printf '%s\n' '=== gateway ==='
kubectl_lab get gatewayclass cilium 2>/dev/null || true
kubectl_lab get gateway -A 2>/dev/null || true
kubectl_lab get httproute -A 2>/dev/null || true
printf '%s\n' '=== load balancers ==='
kubectl_lab get svc -A | grep -E 'LoadBalancer|NAME' || true
printf '%s\n' '=== cilium ==='
kubectl_lab get ciliuml2announcementpolicy,ciliumloadbalancerippool 2>/dev/null || true
