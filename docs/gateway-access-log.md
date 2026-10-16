# Gateway HTTP Access Log

Cilium Gateway API 的 HTTP 访问日志：`CiliumGatewayClassConfig`（CGCC）定义 JSON 字段模板，
Envoy DaemonSet（`kube-system/cilium-envoy`，2 副本）以 JSON 写 stdout，`kubectl logs` 直接可查，
后续可接 Grafana Alloy → VictoriaLogs 集中采集。

## 配置位置

- `config/gateway/gateway.yaml.in` 末尾：`CiliumGatewayClassConfig/cilium-gateway/gateway-access-log`（19 个 JSON 字段）
- 同文件 `GatewayClass/cilium` 的 `spec.parametersRef` 指向上述 CGCC —— 作用于**所有**使用 `cilium` GatewayClass 的 Gateway（当前仅 `lab-gateway`）

字段与集群 live spec 完全一致（2026-10-16 验证，Cilium 1.20.2 / Gateway API CRD v1.5.0）。

## 部署 / 重放

`config/gateway/gateway.yaml.in` 随常规流程渲染部署（`make bootstrap`），
`parametersRef` 为 Helm 管理对象上的追加字段，`helm upgrade --reuse-values` 的 3-way merge 不会回退它。

单独重放：

```bash
scripts/render.sh   # 或直接渲染出 rendered/gateway-api.yaml
kubectl apply -f rendered/gateway-api.yaml
kubectl get ciliumgatewayclassconfig gateway-access-log -n cilium-gateway   # ACCEPTED 应为 True
```

## 查询

**注意：cilium-envoy 是 2 副本 DaemonSet，`kubectl logs -l` 默认只取 1 个 Pod 且无任何提示，
必须加 `--max-log-requests` 才能全量展开**（单 Pod 查不到会误判"未开启"）。

```bash
# 实时全部
kubectl logs -n kube-system -l k8s-app=cilium-envoy --all-containers=true \
  --prefix=true --max-log-requests=10 --tail=0 -f

# 最近 500 条（仅 JSON，过滤 lab-gateway）
kubectl logs -n kube-system -l k8s-app=cilium-envoy --max-log-requests=10 --tail=500 \
  | jq -Rr 'fromjson? | select(.gateway == "lab-gateway")'

# 5xx
kubectl logs -n kube-system -l k8s-app=cilium-envoy --max-log-requests=10 --tail=1000 \
  | jq -Rr 'fromjson? | select((.status|tonumber?) >= 500)'

# 耗时 > 1s
kubectl logs -n kube-system -l k8s-app=cilium-envoy --max-log-requests=10 --tail=1000 \
  | jq -Rr 'fromjson? | select((.duration_ms|tonumber?) > 1000)'
```

端到端验证（2026-10-16 实测记录）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: gitea.kbsonlong.com' \
  "http://192.168.3.243/access-log-probe-$RANDOM"
# → 404，随后 Envoy stdout 出现完整 JSON 条目（status=404, duration_ms≈10, 19 字段齐全）
```

真实流量示例（Gitea 首页，经 Cloudflare Tunnel）：

```json
{"client_ip":"192.168.3.18","duration_ms":12,"gateway":"lab-gateway","host":"gitea.kbsonlong.com",
 "method":"GET","status":200,"upstream_host":"10.42.0.213:80","upstream_service_time":"9",
 "x_forwarded_for":"<公网客户端IP>,192.168.3.18"}
```

## 真实客户端 IP

- 入站链路：Cloudflare Edge → cloudflared（VM100, 192.168.3.18）→ Gateway VIP 192.168.3.243。
  `client_ip` 恒为 tunnel IP（192.168.3.18），**不代表终端用户**。
- `x_forwarded_for` 首个 IP 即真实公网客户端 IP（实测如上）。
- 可选增强（未启用）：`cf_connecting_ip: "%REQ(CF-CONNECTING-IP)%"`——仅当请求确经 Cloudflare 时可信，
  直连 Gateway 的请求可伪造该头，不要作为唯一依据。
- `upstream_service_time` 来自上游响应头，非必在；与 `duration_ms` 之差 ≠ Gateway 自身开销。
- 如未来需按真实 IP 限流/访问控制，再评估 `gatewayAPI.xffNumTrustedHops` 与 `externalTrafficPolicy`，
  日志采集本身无需改动。

## 回滚

```bash
kubectl patch gatewayclass cilium --type=json \
  -p='[{"op":"remove","path":"/spec/parametersRef"}]'
kubectl delete ciliumgatewayclassconfig gateway-access-log -n cilium-gateway
kubectl get gateway -A   # lab-gateway 仍应 PROGRAMMED True
```

（同时从 `config/gateway/gateway.yaml.in` 移除对应段落，避免下次部署重新开启。）

## 注意事项

- CRD 为 `v2alpha1`，Cilium 升级前检查兼容性。
- 每个 HTTP 请求一条日志：高 QPS 场景评估 stdout、容器运行时日志轮转与后续 Alloy 采集开销。
- `uri` 含查询串（可能带 token 等敏感参数），公网服务注意脱敏。
- 当前 Helm 基线：`gatewayAPI.useRemoteAddress: true`、`xffNumTrustedHops: 0`、`externalTrafficPolicy: Cluster`，保持不动。
