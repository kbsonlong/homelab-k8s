#!/usr/bin/env bash
# 不经过 waf-console 直接解封：scripts/waf-unblock.sh <ip> | --all
# 优先用 waf-console（封禁中心或 DELETE /api/v1/blocks/<ip>），它会同时写审计记录。
set -Eeuo pipefail
ip="${1:?usage: $0 <ip>|--all}"
NS=waf ROUTE=waf-blocklist
if [[ "$ip" == --all ]]; then
  # 删除后 waf-console 会在 30 秒内重建一条空的占位路由
  kubectl -n "$NS" delete httproute "$ROUTE" --ignore-not-found && echo "已清空全部封禁"; exit 0
fi
command -v jq >/dev/null || { echo "需要 jq"; exit 2; }
kubectl -n "$NS" get httproute "$ROUTE" -o json | jq --arg ip "$ip" '
  .spec.rules |= (map(.matches |= map(select(([.headers[]?.value] | index($ip)) | not)))
                  | map(select(.matches | length > 0)))
  # rules 不能为空（为空时会匹配全部请求），所以补一条永不命中的占位规则
  | if (.spec.rules | length) == 0 then
      .spec.rules = [{matches: [{path: {type: "PathPrefix", value: "/"},
                       headers: [{type: "Exact", name: "X-Waf-Blocklist-Sentinel", value: "never-match-0b7c1e"}]}],
                      backendRefs: [{name: "crs-detector", port: 8082}]}]
    else . end
  | .metadata.annotations["waf.kbsonlong.com/expires"] |= ((. // "{}") | fromjson | del(.[$ip]) | tojson)
  | del(.status)' | kubectl replace -f -
echo "已解封 $ip"
