# 可观测性栈：VictoriaMetrics 全家桶 + Grafana（2026-10-16）

集群内单实例可观测性：metrics / logs / traces 三条链路 + Grafana 看板。
部署方式：helm release `vmk8s`（chart `victoria-metrics-k8s-stack` 0.95.2），namespace `monitoring`。

## 架构

| 信号 | 采集 | 存储 | 容量/保留 | 暴露 |
|---|---|---|---|---|
| Metrics | vmagent（VMServiceScrape/VMNodeScrape 自动发现，默认已抓 kubelet/ksm/node-exporter/coredns 等） | vmsingle (v1.153.0) | PVC 6Gi，14d | svc :8428（vmui） |
| Logs | vlagent（k8sCollector 经 K8s API 抓全集群容器日志，无需 DaemonSet） | vlsingle (v1.53.0) | PVC 4Gi，7d | svc :9428 |
| Traces | 应用直发 OTLP gRPC | vtsingle (v0.10.0) | PVC 2Gi，7d | svc :10428（HTTP/Jaeger）+ `vtsingle-otlp-grpc` :4317 |
| 看板 | — | Grafana（PVC 1Gi） | — | HTTPRoute `grafana.${BASE_DOMAIN}` → 共享 lab-gateway (**192.168.3.243**) |

- 数据组件全部 `nodeSelector: kubernetes.io/hostname: colima`（local-path PVC 节点绑定 + 内存充足）。
- vmalert / alertmanager / defaultRules 已关闭：告警链路独立于集群（ntfy @ VM100），先看板后告警。
- Grafana 是七层 HTTP 服务，按集群架构约定走共享 Gateway（`lab-gateway` @ 192.168.3.243）+ HTTPRoute（`helm/victoriametrics/grafana-httproute.yaml.in`，`scripts/render.sh` 替换 `${BASE_DOMAIN}`）；**LB VIP 池只留给真实 TCP/UDP 四层服务**，勿为 HTTP 服务申请独立 LoadBalancer。
- LAN 内未配 DNS：`curl --resolve grafana.kbsonlong.com:80:192.168.3.243 ...` 或在客户端 hosts 加记录。
- ⚠️ **浏览器直访必须先加 hosts，否则重定向循环**：公网 Cloudflare 上存在 `*.kbsonlong.com` 通配解析（指向 CF 边缘），hosts 未加时浏览器解析去 CF，被 301 → https 后无源站路由（cf-tunnel 只配了 ntfy），表现为 ERR_TOO_MANY_REDIRECTS。修复（每台客户端）：
  `echo '192.168.3.243 grafana.kbsonlong.com gitea.kbsonlong.com argocd.kbsonlong.com' | sudo tee -a /etc/hosts`
  **勿加 ntfy.kbsonlong.com**（走公网 cf-tunnel，加了会断推送）。Chrome 开启「安全 DNS」可能绕过 hosts，必要时关闭。
- 磁盘预算 13Gi / colima root 盘 ~17G 可用；local-path 是 sparse 文件，`df` 短期看不出增长属正常。

## 部署 / 升级

values 正本：`helm/victoriametrics/values.yaml`；独立 Service：`helm/victoriametrics/vtsingle-otlp-grpc-svc.yaml`。

```bash
export HTTPS_PROXY=http://192.168.3.16:1087 HTTP_PROXY=http://192.168.3.16:1087   # OCI chart 走代理
helm upgrade --install vmk8s oci://ghcr.io/victoriametrics/helm-charts/victoria-metrics-k8s-stack \
  --version 0.95.2 -n monitoring --create-namespace \
  -f helm/victoriametrics/values.yaml
kubectl apply -f helm/victoriametrics/vtsingle-otlp-grpc-svc.yaml
./scripts/render.sh && kubectl apply -f rendered/grafana-httproute.yaml   # 七层入口挂共享 lab-gateway
```

## 已知坑（重新部署必读）

1. **sync-job 被墙**：post-install hook 会从 raw.githubusercontent.com 拉 dashboards/rules，集群内直连超时 → 整个 release 状态 `failed`（核心组件其实已 Running）。解法：values 里 `syncJob.env` 注 `HTTPS_PROXY=http://192.168.3.16:1087`（官方 air-gapped Option 2），已含在 values 正本。
2. **vtsingle OTLP gRPC 默认禁用且强制 TLS**：只加 `otlpGRPCListenAddr: ":4317"` 会 CrashLoop（fatal: `-otlpGRPC.tlsKeyFile is required`），必须同时加 `otlpGRPC.tls: "false"`（内网明文）。
3. **OTLP 仅 gRPC**：无 HTTP ingestion，`POST /opentelemetry/v1/traces` 返回 `unsupported path`。Jaeger 查询 API（`/select/jaeger/api/traces`）数据可见有分钟级延迟，验证时别急着判失败。
4. **retentionPeriod 单位陷阱**：vmsingle/vlsingle 无单位 = 月；vtsingle 无单位 = 天。
5. **4317 不在 helm svc 里**：operator 生成的 svc 只暴露 10428；4317 用独立 Service `vtsingle-otlp-grpc` 路由（本仓库 manifest），helm upgrade 不会动它。

## 验证清单

```bash
kubectl -n monitoring get pods          # 10 Running + sync-job Completed
kubectl -n monitoring get pvc           # 4 个 Bound (6G/4G/2G/1G)
curl --resolve grafana.kbsonlong.com:80:192.168.3.243 http://grafana.kbsonlong.com/api/health    # 200

# 三数据源健康（Grafana API）
GW='--resolve grafana.kbsonlong.com:80:192.168.3.243'
PW="$(kubectl -n monitoring get secret vmk8s-grafana -o jsonpath='{.data.admin-user}' | base64 -d):$(kubectl -n monitoring get secret vmk8s-grafana -o jsonpath='{.data.admin-password}' | base64 -d)"
for uid in VictoriaMetrics VictoriaLogs VictoriaTraces; do curl -su "$PW" $GW http://grafana.kbsonlong.com/api/datasources/uid/$uid/health; echo; done

# 数据面（集群内临时 pod）
kubectl run nc --rm -i --restart=Never --image=curlimages/curl:8.16.0 -- \
  curl -s 'http://vmsingle-vmk8s-victoria-metrics-k8s-stack.monitoring.svc:8428/api/v1/query?query=count(up)'
# 期望 ~21 targets；logs 查 vlsingle :9428/select/logsql/query?query=*；traces 查 vtsingle :10428/select/jaeger/api/services
```

## 使用

- **Grafana**: `http://grafana.kbsonlong.com`（经 lab-gateway 192.168.3.243，LAN 内需 hosts/--resolve），admin 密码见上；预置 20+ k8s dashboards（sync-job 生成）。
- **应用埋点 traces**: OTLP gRPC → `vtsingle-otlp-grpc.monitoring.svc:4317`（insecure）。
- **应用 metrics 接入**: 创建 `VMServiceScrape` CR 即被 vmagent 自动抓取，无需改 vmagent。
- **日志**: 零配置，vlagent 自动抓全部 namespace 容器日志。

## 回滚

```bash
helm uninstall vmk8s -n monitoring
kubectl delete svc vtsingle-otlp-grpc -n monitoring
kubectl delete ns monitoring          # local-path Reclaim=Delete，PVC 数据一并清除（不可恢复）
```
