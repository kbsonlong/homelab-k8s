#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"

"$ROOT_DIR/scripts/check.sh"

kubectl_lab wait --for=condition=Available deployment --all -n "$ARGOCD_NAMESPACE" --timeout=300s 2>/dev/null || true
kubectl_lab wait --for=condition=Available deployment --all -n "$GITEA_NAMESPACE" --timeout=300s 2>/dev/null || true

class_ok=$(kubectl_lab get gatewayclass cilium -o jsonpath='{.status.conditions[?(@.type=="Accepted")].status}' 2>/dev/null || true)
gateway_ok=$(kubectl_lab -n "$GATEWAY_NAMESPACE" get gateway lab-gateway -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null || true)
[[ "$class_ok" == True ]] || die "GatewayClass not Accepted: $class_ok"
[[ "$gateway_ok" == True ]] || die "Gateway not Programmed: $gateway_ok"

for route in argocd/argocd-ui gitea/gitea-http; do
  ns=${route%/*}; name=${route#*/}
  accepted=$(kubectl_lab -n "$ns" get httproute "$name" -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}')
  refs=$(kubectl_lab -n "$ns" get httproute "$name" -o jsonpath='{.status.parents[0].conditions[?(@.type=="ResolvedRefs")].status}')
  [[ "$accepted" == True && "$refs" == True ]] || die "HTTPRoute $route invalid: Accepted=$accepted ResolvedRefs=$refs"
done

for svc in argocd/argo-cd-argocd-server gitea/gitea-http gitea/gitea-ssh; do
  ns=${svc%/*}; name=${svc#*/}
  type=$(kubectl_lab -n "$ns" get svc "$name" -o jsonpath='{.spec.type}')
  case "$name" in
    gitea-ssh) [[ "$type" == LoadBalancer ]] || die "$svc must remain LoadBalancer" ;;
    *) [[ "$type" == ClusterIP ]] || die "$svc must be ClusterIP, got $type" ;;
  esac
done

gw_ip=$(kubectl_lab -n "$GATEWAY_NAMESPACE" get gateway lab-gateway -o jsonpath='{.status.addresses[0].value}')
[[ -n "$gw_ip" ]] || die 'Gateway has no VIP'

for host in "gitea.${BASE_DOMAIN}" "argocd.${BASE_DOMAIN}"; do
  code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' -H "Host: $host" "http://$gw_ip/" --max-time 10)
  case "$host" in
    gitea.*) [[ "$code" == 2* ]] || die "$host returned HTTP $code" ;;
    argocd.*) [[ "$code" == 2* || "$code" == 3* ]] || die "$host returned HTTP $code" ;;
  esac
done

printf 'VERIFY OK: Gateway VIP=%s; HTTP routes healthy; only Gitea SSH remains L4 LB\n' "$gw_ip"
