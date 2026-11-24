#!/usr/bin/env bash
# P0 验收：主链路不受影响 + 攻击样本能在 VictoriaLogs 里查到（lab-gateway 只有 HTTP :80 listener）
#   SITES="gitea.kbsonlong.com argocd.kbsonlong.com" ./scripts/waf-verify.sh
set -Eeuo pipefail
VIP="${VIP:-192.168.3.243}"
SITES="${SITES:-gitea.kbsonlong.com argocd.kbsonlong.com grafana.kbsonlong.com}"
VL_SVC="${VL_SVC:-svc/vlsingle-vmk8s-victoria-metrics-k8s-stack}"   # kubectl -n monitoring get svc | grep vlsingle
VL_NS="${VL_NS:-monitoring}"
WAIT="${WAIT:-20}"
PASS=0 FAIL=0
ok(){ echo "  ✔ $*"; PASS=$((PASS+1)); }  bad(){ echo "  ✘ $*"; FAIL=$((FAIL+1)); }

echo "== 1. crs-detector 状态"
kubectl -n waf rollout status deploy/crs-detector --timeout=60s >/dev/null && ok "crs-detector Ready" || bad "crs-detector 未就绪"
kubectl -n waf logs deploy/crs-detector --tail=200 2>/dev/null | grep -qiE 'emerg|ModSecurity.*error' \
  && bad "日志中有 nginx/ModSecurity 错误（kubectl -n waf logs deploy/crs-detector）" || ok "启动日志无 emerg/error"

MARK="wafverify$(date +%s)$RANDOM"
echo "== 2. 主链路（标记 ${MARK}）"
for h in $SITES; do
  t0=$(date +%s%N)
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$h:80:$VIP" \
    "http://$h/?id=1%27%20OR%20%271%27=%271&q=%3Cscript%3Ealert(1)%3C/script%3E&$MARK=1" || echo 000)
  ms=$(( ($(date +%s%N)-t0)/1000000 ))
  # 旁路模式下攻击请求不会被拦，状态码应该和正常访问一样（不是 403）
  [[ "$code" != "000" && "$code" != "403" ]] && ok "$h → HTTP $code（${ms}ms），未被拦截" || bad "$h → HTTP $code"
  curl -sk -o /dev/null --max-time 8 --resolve "$h:80:$VIP" "http://$h/.env?$MARK=1" || true
done

echo "== 3. VictoriaLogs 中的检测事件（等待 ${WAIT}s）"
sleep "$WAIT"
kubectl -n "$VL_NS" port-forward "$VL_SVC" 19428:9428 >/dev/null 2>&1 & PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 2
q="{kubernetes.namespace=\"waf\"} \"$MARK\" | stats count() hits"
hits=$(curl -s --max-time 10 http://127.0.0.1:19428/select/logsql/query --data-urlencode "query=$q" \
  --data-urlencode "start=10m" | jq -r '.hits // 0' 2>/dev/null || echo 0)
(( hits > 0 )) && ok "查到 ${hits} 条检测事件" || bad "没查到事件：检查 RequestMirror 是否生效、vlagent 是否采集 waf 命名空间"

for h in $SITES; do
  q="{kubernetes.namespace=\"waf\"} \"$MARK\" \"$h\" | stats count() hits"
  n=$(curl -s --max-time 10 http://127.0.0.1:19428/select/logsql/query --data-urlencode "query=$q" \
    --data-urlencode "start=10m" | jq -r '.hits // 0' 2>/dev/null || echo 0)
  (( n > 0 )) && ok "$h 有镜像事件" || bad "$h 没有镜像事件（HTTPRoute 未挂镜像或 ReferenceGrant 缺失）"
done

echo "== 4. 客户端 IP 是否正确"
q="{kubernetes.namespace=\"waf\"} \"$MARK\" | unpack_json fields (transaction) | unpack_json from transaction fields (client_ip) | stats by (client_ip) count()"
curl -s --max-time 10 http://127.0.0.1:19428/select/logsql/query --data-urlencode "query=$q" --data-urlencode "start=10m" \
  | jq -r '"  client_ip=\(.client_ip)"' 2>/dev/null || true
echo "  ↑ 应该是本机的 LAN IP；如果是节点或 Pod IP，说明 trusted-proxies.conf 没配对"

echo; echo "通过 $PASS  失败 $FAIL"
exit $(( FAIL > 0 ))
