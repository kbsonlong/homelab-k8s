#!/usr/bin/env bash
# 从 .env 生成 waf-console 需要的 Secret，幂等，不写进 Git
set -Eeuo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[[ -f .env ]] && set -a && . ./.env && set +a

req() { [[ -n "${!1:-}" ]] || { echo "缺少 .env 变量: $1" >&2; exit 2; }; }
for v in WAF_GIT_TOKEN WAF_ARGOCD_TOKEN WAF_OAUTH_CLIENT_ID WAF_OAUTH_CLIENT_SECRET; do req "$v"; done
WAF_ALERT_TOKEN="${WAF_ALERT_TOKEN:-$(openssl rand -hex 24)}"
WAF_OAUTH_COOKIE_SECRET="${WAF_OAUTH_COOKIE_SECRET:-$(openssl rand -base64 32 | tr -d '\n' | head -c 32)}"

kubectl create ns waf --dry-run=client -o yaml | kubectl apply -f -

kubectl -n waf create secret generic waf-console \
  --from-literal=GIT_TOKEN="$WAF_GIT_TOKEN" \
  --from-literal=ARGOCD_TOKEN="$WAF_ARGOCD_TOKEN" \
  --from-literal=ALERT_TOKEN="$WAF_ALERT_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n waf create secret generic waf-console-oauth \
  --from-literal=OAUTH2_PROXY_CLIENT_ID="$WAF_OAUTH_CLIENT_ID" \
  --from-literal=OAUTH2_PROXY_CLIENT_SECRET="$WAF_OAUTH_CLIENT_SECRET" \
  --from-literal=OAUTH2_PROXY_COOKIE_SECRET="$WAF_OAUTH_COOKIE_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -

# vmalert 调用 webhook 时用的 token
kubectl -n monitoring create secret generic waf-alert-token \
  --from-literal=token="$WAF_ALERT_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "OK: waf/waf-console, waf/waf-console-oauth, monitoring/waf-alert-token"
