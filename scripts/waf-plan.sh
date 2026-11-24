#!/usr/bin/env bash
# 只读：渲染 config/waf，做规则 ID 冲突检查和 kubectl diff，不修改集群
set -Eeuo pipefail
cd "$(dirname "$0")/.."
out=$(mktemp); trap 'rm -f "$out"' EXIT

echo "== kustomize build config/waf"
kubectl kustomize config/waf > "$out"
echo "   $(grep -c '^kind:' "$out") 个资源"

echo "== 规则 ID 冲突检查"
dups=$(grep -rhoE 'id:[0-9]+' config/waf/sites config/waf/custom | sort | uniq -d || true)
if [[ -n "$dups" ]]; then echo "   重复 ID：$dups" >&2; exit 1; fi
bad=$(grep -rhoE 'id:[0-9]+' config/waf/sites config/waf/custom | cut -d: -f2 | awk '$1<1000000 || $1>1199999' || true)
if [[ -n "$bad" ]]; then echo "   超出 1000000-1199999 的 ID：$bad" >&2; exit 1; fi
echo "   OK"

echo "== kubectl diff"
kubectl diff -f "$out" || true   # diff 有差异时退出码为 1，不算失败
scripts/render.sh >/dev/null
kubectl diff -f rendered/gateway-api.yaml -f apps/observability/grafana-httproute.yaml || true
kubectl kustomize apps/waf-console/deploy >/dev/null && echo "   apps/waf-console/deploy 渲染 OK"
