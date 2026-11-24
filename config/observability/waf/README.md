# WAF 可观测性

| 文件 | 作用 |
|---|---|
| `vmalert-logs.yaml` | 独立的 vmalert：LogsQL 规则查 VictoriaLogs，指标规则查 VictoriaMetrics |
| `vmalertmanager.yaml` | 路由：`waf_action=block` 发到 waf-console（执行封禁），其他告警发到 ntfy `waf` 主题 |
| `vmrule-waf-logs.yaml` | 封禁类：WafHighAnomaly(1h) / WafCriticalAttack(24h) / WafSensitiveProbe(6h)；信号类：攻击突增、日志断流、检测器报错 |
| `vmrule-waf-metrics.yaml` | 检测器宕机/重启/OOM/CPU 节流、控制台宕机、Argo CD 应用异常、Gateway P99 延迟 |
| `vmservicescrape.yaml` | 采集 Argo CD 应用指标和 cilium-envoy 指标（可选） |
| `dashboards/waf.json` | Grafana 看板，分概览 / 趋势 / Top N / 事件明细四块 |

## 告警 → 封禁链路

```
crs-detector JSON 日志 ─► VictoriaLogs ─► vmalert(waf) 每分钟执行 LogsQL
   stats by (client_ip) ─► 告警 label client_ip、waf_action=block、waf_ttl
   ─► vmalertmanager(waf) ─► POST waf-console /api/v1/alerts（Bearer token）
   ─► 白名单 / 上限 / 自动封禁开关检查 ─► HTTPRoute waf/waf-blocklist（按 CF-Connecting-IP 返回 403）─► ntfy 通知
```

## 阈值

| 告警 | 条件 | 动作 |
|---|---|---|
| WafHighAnomaly | 同一 IP 5 分钟内命中 ≥ 10 次 | 封 1h |
| WafCriticalAttack | 同一 IP 5 分钟内命中 CRITICAL 级别的 913/930-934/941-944 规则 | 封 24h |
| WafSensitiveProbe | 5 分钟内命中自定义规则 1100001 ≥ 3 次 | 封 6h |
| WafAttackSurge | 全站 5 分钟内命中 ≥ 50 次 | 通知 |
| WafNoEvents | 24 小时内没有任何日志（持续 1h） | 通知 |

上线建议：前 1～2 周把 waf-console 的 `BLOCK_AUTO` 设为 false。这段时间告警仍会发到 waf-console，但 waf-console 只记录、不封禁（返回 skipped）。先在看板里确认没有误判，再打开自动封禁。

## 依赖与 TODO

- **VictoriaLogs 要把 JSON 日志展开成字段**：规则和看板直接用 `transaction.client_ip` 这类字段。`scripts/waf-observability-verify.sh` 第 1 步会检查。如果没有展开，在 vlagent 上开启 JSON 解析，或者在查询里加 `| unpack_json`。
- **Grafana 需要安装 VictoriaLogs 数据源插件**：`victoriametrics-logs-datasource`。看板 panel 的 `queryType`（stats / statsRange / instant）请对照插件版本核对。
- **Service 地址**：vlsingle、vmsingle、ntfy 的 Service 名按 chart 默认值猜测，需要核对。
- **ntfy 版本**：`?template=alertmanager` 需要较新的 ntfy 版本，旧版本会收到原始 JSON。
- **Argo CD 指标**：需要在 argo-cd chart 里开启 `controller.metrics.enabled`。
- **Gateway 延迟指标**：需要在 Cilium values 里开启 `envoy.prometheus.enabled`。
- **IP 过滤变量**：看板里的站点、IP 文本框用正则匹配，留空表示匹配全部。如果插件不支持 `${var:regex}` 格式，改成 `${var}`。
