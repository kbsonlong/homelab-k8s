#!/usr/bin/env bash
# 校验可观测性链路：日志字段 → LogsQL 规则 → vmalert → alertmanager → waf-console / ntfy
set -Eeuo pipefail
NS=monitoring
VL_SVC="${VL_SVC:-svc/vlsingle-vmk8s-victoria-metrics-k8s-stack}"
ok(){ echo "  ✔ $*"; } bad(){ echo "  ✘ $*"; FAIL=1; } FAIL=0

kubectl -n "$NS" port-forward "$VL_SVC" 19428:9428 >/dev/null 2>&1 & PF1=$!
trap 'kill $PF1 2>/dev/null || true' EXIT; sleep 2
vl(){ curl -s --max-time 15 http://127.0.0.1:19428/select/logsql/query --data-urlencode "query=$1" --data-urlencode "start=${2:-24h}"; }

echo "== 1. 审计日志字段（规则和看板依赖这些字段）"
row=$(vl '{kubernetes.namespace="waf", kubernetes.container_name="crs-detector"} transaction.messages:* | limit 1' 7d | head -n1)
if [[ -z "$row" ]]; then
  bad "7 天内没有带 transaction.messages 的日志：先跑 make waf-verify 产生事件；或者 JSON 没有被展开成字段（见下一步）"
else
  for f in transaction.client_ip transaction.request.uri transaction.request.headers.Host transaction.messages; do
    echo "$row" | jq -e --arg f "$f" 'has($f)' >/dev/null && ok "字段 $f 存在" || bad "缺少字段 $f"
  done
  echo "$row" | jq -r '.["transaction.messages"]' | jq -e '.[0].details.ruleId' >/dev/null 2>&1 \
    && ok "transaction.messages 是 JSON 数组，unroll/unpack_json 可以用" || bad "transaction.messages 结构和预期不一致"
fi
raw=$(vl '{kubernetes.namespace="waf", kubernetes.container_name="crs-detector"} "\"transaction\"" | limit 1 | fields _msg' 7d | head -n1)
[[ -n "$raw" && -z "$row" ]] && echo "  提示：日志在 _msg 里但没有被解析。可以在 vlagent 上开启 JSON 解析，或者在每条查询前加 '| unpack_json'"

echo "== 2. 逐条执行告警规则里的 LogsQL"
kubectl get vmrule -n "$NS" waf-logs -o json | jq -r '.spec.groups[].rules[] | "\(.alert)\t\(.expr|gsub("\n";" "))"' |
while IFS=$'\t' read -r name expr; do
  out=$(vl "$expr" 1h); if echo "$out" | grep -q '"error"\|cannot parse'; then bad "$name 语法错误：$(echo "$out" | head -c 200)"; else ok "$name 可执行（返回 $(echo -n "$out" | grep -c . || true) 行）"; fi
done

echo "== 3. 告警组件"
kubectl -n "$NS" get vmalert waf -o jsonpath='{.status.updateStatus}{"\n"}' 2>/dev/null | grep -qi operational && ok "VMAlert/waf operational" || bad "VMAlert/waf 未就绪"
kubectl -n "$NS" get vmalertmanager waf -o jsonpath='{.status.updateStatus}{"\n"}' 2>/dev/null | grep -qi operational && ok "VMAlertmanager/waf operational" || bad "VMAlertmanager/waf 未就绪"
kubectl -n "$NS" logs deploy/vmalert-waf --tail=200 2>/dev/null | grep -iE 'error|cannot' | tail -n 5 | sed 's/^/    /' || true

echo "== 4. 端到端：发一条测试告警到 alertmanager（只发 ntfy，不触发封禁）"
kubectl -n "$NS" port-forward svc/vmalertmanager-waf 19093:9093 >/dev/null 2>&1 & PF2=$!; sleep 2
curl -s -XPOST http://127.0.0.1:19093/api/v2/alerts -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"WafTest","severity":"info"},"annotations":{"summary":"waf-observability-verify 测试通知"}}]' \
  && ok "测试告警已发送：几十秒内 ntfy 主题 waf 应该收到通知"
kill $PF2 2>/dev/null || true

echo "== 5. Grafana 看板"
kubectl -n "$NS" get cm grafana-dashboard-waf >/dev/null 2>&1 && ok "ConfigMap grafana-dashboard-waf 存在（Grafana → Dashboards → WAF）" || bad "看板 ConfigMap 不存在"
kubectl -n "$NS" get pods -l app.kubernetes.io/name=grafana -o json | jq -r '.items[0].spec.containers[].name' | grep -q dashboard \
  && ok "Grafana 有 dashboard sidecar" || echo "  ! 没找到 dashboard sidecar，需要手动导入 dashboards/waf.json"
echo "  ! 看板依赖 VictoriaLogs 数据源插件（victoriametrics-logs-datasource），确认 Grafana 已安装"
exit $FAIL
