#!/usr/bin/env bash
# 验证 Gateway（Envoy）层按 CF-Connecting-IP 封禁是否生效。
#
# 做法：临时创建一条和 waf-console 生成的 waf-blocklist 结构完全一样的测试路由
# waf/waf-blocklist-verify，只封一个文档保留地址（默认 203.0.113.77），不改动真实名单。
# 退出时（包括 Ctrl-C）自动删除测试路由。
#
# 用法：make waf-verify-block    [TEST_HOST=gitea.kbsonlong.com TEST_IP=203.0.113.77]
set -Eeuo pipefail
TEST_HOST="${TEST_HOST:-gitea.kbsonlong.com}"
TEST_IP="${TEST_IP:-203.0.113.77}"
OTHER_IP="${OTHER_IP:-203.0.113.78}"
VIP="${VIP:-192.168.3.243}"
GW="${GW:-lab-gateway}" GW_NS="${GW_NS:-cilium-gateway}" GW_SECTION="${GW_SECTION:-http}"
ROUTE=waf-blocklist-verify
WAIT="${WAIT:-5}"
PASS=0 FAIL=0
ok(){ echo "  ✔ $*"; PASS=$((PASS+1)); }  bad(){ echo "  ✘ $*"; FAIL=$((FAIL+1)); }
for b in kubectl curl; do command -v "$b" >/dev/null || { echo "缺少 $b"; exit 2; }; done

probe(){ # $1 = CF-Connecting-IP（空表示不带）
  local h=(); [[ -n "${1:-}" ]] && h=(-H "CF-Connecting-IP: $1")
  curl -s -o /tmp/waf-verify-body -w '%{http_code}' --max-time 6 --resolve "$TEST_HOST:80:$VIP" "${h[@]}" "http://$TEST_HOST/" || echo 000
}
cleanup(){ kubectl -n waf delete httproute "$ROUTE" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

echo "== 0. 前置检查"
kubectl -n waf get svc crs-detector -o jsonpath='{.spec.ports[*].port}' | grep -qw 8082 \
  && ok "crs-detector Service 暴露了 8082（403 响应端口）" || { bad "crs-detector 没有 8082 端口，先同步 Argo CD 应用 waf"; exit 1; }
kubectl get httproute -A -o json | grep -q "\"$TEST_HOST\"" && ok "已存在 $TEST_HOST 的业务路由" || { bad "找不到 $TEST_HOST 的 HTTPRoute"; exit 1; }

echo "== 1. 基线（未封禁）"
c=$(probe "$TEST_IP"); [[ "$c" != 000 && "$c" != 403 ]] && ok "带 CF-Connecting-IP=$TEST_IP 访问 → HTTP $c" || { bad "基线就失败（HTTP $c）"; exit 1; }

echo "== 2. 创建测试封禁路由（只封 $TEST_IP）"
kubectl apply -f - >/dev/null <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: $ROUTE, namespace: waf, labels: {app.kubernetes.io/part-of: waf}}
spec:
  parentRefs: [{name: $GW, namespace: $GW_NS, sectionName: $GW_SECTION}]
  hostnames: ["$TEST_HOST"]
  rules:
    - matches:
        - path: {type: PathPrefix, value: /}
          headers: [{type: Exact, name: CF-Connecting-IP, value: "$TEST_IP"}]
      backendRefs: [{name: crs-detector, port: 8082}]
EOF
sleep "$WAIT"
acc=$(kubectl -n waf get httproute "$ROUTE" -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}')
res=$(kubectl -n waf get httproute "$ROUTE" -o jsonpath='{.status.parents[0].conditions[?(@.type=="ResolvedRefs")].status}')
[[ "$acc" == True ]] && ok "路由已被 Gateway 接受" || bad "路由 Accepted=$acc（检查 Gateway allowedRoutes）"
[[ "$res" == True ]] && ok "backendRef 已解析" || bad "ResolvedRefs=$res"

echo "== 3. 被封 IP 应得到 403"
c=$(probe "$TEST_IP")
if [[ "$c" == 403 ]] && grep -q "Forbidden by WAF" /tmp/waf-verify-body; then ok "CF-Connecting-IP=$TEST_IP → 403 Forbidden by WAF"
else bad "CF-Connecting-IP=$TEST_IP → HTTP $c（Cilium 没有按 header match 数量决定优先级？）"; fi

echo "== 4. 其他来源不受影响"
c=$(probe "$OTHER_IP"); [[ "$c" != 403 && "$c" != 000 ]] && ok "CF-Connecting-IP=$OTHER_IP → HTTP $c" || bad "其他 IP 被误伤（HTTP $c）"
c=$(probe "");          [[ "$c" != 403 && "$c" != 000 ]] && ok "LAN 直连（不带头）→ HTTP $c" || bad "LAN 直连被误伤（HTTP $c）"

echo "== 5. 封禁日志"
sleep 2
kubectl -n waf logs deploy/crs-detector --since=1m 2>/dev/null | grep -q "\"waf_deny\":true.*$TEST_IP" \
  && ok "crs-detector 记录了 waf_deny 日志" || bad "没找到 waf_deny 日志"

echo "== 6. 删除测试路由后恢复"
cleanup; sleep "$WAIT"
c=$(probe "$TEST_IP"); [[ "$c" != 403 && "$c" != 000 ]] && ok "解除后恢复（HTTP $c）" || bad "解除后仍被拒（HTTP $c）"

echo; echo "通过 $PASS  失败 $FAIL"
(( FAIL == 0 )) && echo "Gateway 层封禁可用。外网真实链路还需从手机热点访问一次，确认 Cloudflare 带了 CF-Connecting-IP（看 Gateway 访问日志 cf_connecting_ip 字段）。"
exit $(( FAIL > 0 ))
