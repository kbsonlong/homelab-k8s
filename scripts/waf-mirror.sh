#!/usr/bin/env bash
# 紧急开关镜像：waf-mirror.sh off|on|status
#   off：删除所有带 waf.kbsonlong.com/mirror=true 标签的 HTTPRoute 里的 RequestMirror
#   on ：从 Git（config/gateway/httproute-*.yaml）重新应用
# 注意：如果路由由 Argo CD 管理，off 之后会被 selfHeal 还原，所以脚本会先暂停对应应用的自动同步
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SEL="waf.kbsonlong.com/mirror=true"
cmd="${1:-status}"

routes() { kubectl get httproute -A -l "$SEL" -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}'; }

case "$cmd" in
  status)
    kubectl get httproute -A -l "$SEL" -o json | jq -r '.items[] |
      "\(.metadata.namespace)/\(.metadata.name)\tmirror=\([.spec.rules[].filters[]? | select(.type=="RequestMirror")] | length > 0)"'
    ;;
  off)
    while read -r ns name; do
      [[ -z "$ns" ]] && continue
      app=$(kubectl -n "$ns" get httproute "$name" -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}' | cut -d: -f1)
      if [[ -n "$app" ]] && command -v argocd >/dev/null; then
        echo "暂停 Argo CD 应用 $app 的自动同步"; argocd app set "$app" --sync-policy none
      elif [[ -n "$app" ]]; then
        echo "WARN: $ns/$name 由 Argo CD 应用 $app 管理，请手动暂停自动同步，否则会被还原" >&2
      fi
      patch=$(kubectl -n "$ns" get httproute "$name" -o json | jq -c '[.spec.rules | to_entries[] as $r
        | ($r.value.filters // []) | to_entries[] | select(.value.type=="RequestMirror")
        | {op:"remove", path:"/spec/rules/\($r.key)/filters/\(.key)"}] | reverse')
      [[ "$patch" == "[]" ]] && { echo "$ns/$name 没有镜像，跳过"; continue; }
      kubectl -n "$ns" patch httproute "$name" --type=json -p "$patch"
      echo "已关闭镜像：$ns/$name"
    done < <(routes)
    ;;
  on)
    # argocd-ui / gitea-http 来自 config/gateway/gateway.yaml.in（make render 后 apply）
    # grafana-ui 由 Argo CD 应用 observability 管理（apps/observability/grafana-httproute.yaml）
    scripts/render.sh >/dev/null
    kubectl apply --server-side --force-conflicts -f rendered/gateway-api.yaml
    kubectl apply -f apps/observability/grafana-httproute.yaml
    echo "已恢复镜像。如果之前暂停过 Argo CD 自动同步，请执行：argocd app set observability --sync-policy automated --self-heal"
    ;;
  *) echo "usage: $0 off|on|status" >&2; exit 2 ;;
esac
