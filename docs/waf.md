# WAF（旁路模式）设计与运维

本文描述在 homelab-k8s 中引入基于 [OWASP CRS](https://github.com/coreruleset/coreruleset) 的旁路（Out-of-Band）WAF 与管理后台 `waf-console` 的设计、部署、调优与回滚。

- 状态：设计稿（Draft）
- 适用版本：k3s v1.36.5+k3s1、Cilium 1.20.2、Gateway API v1.5.0
- 相关文档：[architecture.md](architecture.md)、[gateway-access-log.md](gateway-access-log.md)、[observability.md](observability.md)、[vm100-side-router-v2ray-iptables.md](vm100-side-router-v2ray-iptables.md)

---

## 1. 背景与目标

当前 `waf` 命名空间中有 `crs-argocd`、`crs-gitea`、`crs-grafana` 三个独立 CRS 实例，存在以下问题：

- 每个应用一套，资源重复，策略分散；
- 若串联在请求链路上，会额外增加一跳和 CRS 解析耗时，WAF 故障时业务也会跟着不可用。

目标：

| 目标 | 说明 |
|---|---|
| 不影响主链路 | WAF 不在请求路径上，业务延迟基本不变，WAF 故障不影响访问 |
| 统一管理 | 一个检测器覆盖所有 HTTPRoute 站点 |
| 自动处置 | 检测到攻击后，在最外层按 IP 封禁，支持 TTL 和白名单 |
| GitOps | 策略、例外和封禁记录都进 Git，由 Argo CD 同步，可审计、可回滚 |
| 轻量 | 新增组件总内存 < 512Mi，不引入数据库或消息队列 |

非目标：拦截第一个恶意请求，以及检测响应内容（信息泄露）。这两项是旁路模式的固有局限，见第 9 节。

---

## 2. 架构

```
LAN 192.168.3.0/24
      │
VIP 192.168.3.243 ── Cilium Gateway (Envoy)
                         │
        ┌────────────────┼──────────────────────────┐
  主链路（不变）           │              RequestMirror（异步，发完不等待）
        ▼                                            ▼
 gitea / argocd / grafana ...           waf/crs-detector (DetectionOnly)
                                                     │ JSON 审计日志 → stdout
                                      vlagent → VictoriaLogs → Grafana
                                                     │
                                      vmalert (LogsQL 规则) → waf-console webhook
                                                     │
                        ┌────────────────────────────┴───────────────┐
                        ▼                                            ▼
          Cilium Gateway (Envoy)：HTTPRoute waf-blocklist 按 CF-Connecting-IP 返回 403
                        └─────────── ntfy 通知，TTL 到期自动解封 ───────────┘
```

设计原则（与仓库现有约定一致）：

- 7 层入口仍然只有 Gateway VIP `192.168.3.243`，不新增 LoadBalancer；
- 第三方组件用 Helm 安装、固定 values；Gateway/Cilium 资源写成 YAML；脚本只负责编排和验证；
- 密钥只从 `.env` 或 CI secret 渲染，不进 Git；
- 告警统一发到 ntfy。

---

## 3. 组件

| 组件 | 命名空间 | 形态 | 资源 req → limit | 说明 |
|---|---|---|---|---|
| `crs-detector` | waf | Deployment ×1 | 50m/128Mi → 500m/384Mi | `owasp/modsecurity-crs:nginx`，DetectionOnly，上游指向本地 204 dummy |
| `waf-console` | waf | Deployment ×1 | 20m/32Mi → 200m/64Mi | Go 单二进制，无状态；读写 Git、查 LogsQL、读 Argo CD、执行封禁 |
| `waf-blocklist` | waf | HTTPRoute | — | 动态封禁路由，由 waf-console 维护 |
| 复用组件 | — | — | — | Cilium Gateway、vlagent、VictoriaLogs、vmalert、Grafana、Argo CD、ntfy |

检测器 `priorityClassName` 低于业务，资源紧张时优先被驱逐。上线后下线原有的三个 `crs-*`。

---

## 4. 仓库目录

```
homelab-k8s/
├── apps/waf-console/                 # Go 源码 + web/
├── helm/waf/
│   ├── Chart.yaml
│   ├── values.yaml                   # crs-detector + waf-console
│   └── templates/
├── config/
│   ├── waf/
│   │   ├── crs-setup.conf            # 全局 PL、阈值
│   │   ├── sites/<site>/site.yaml    # 结构化站点配置（控制台读写）
│   │   ├── sites/<site>/before-crs.conf
│   │   ├── sites/<site>/after-crs.conf
│   │   ├── custom/rules.conf         # 自定义规则，ID 段 1000000-1099999
│   │   ├── allowlist.yaml            # 永不封禁
│   │   └── blocklist.yaml            # 封禁审计记录
│   ├── gateway/
│   │   ├── httproute-*.yaml          # 增加 RequestMirror filter
│   │   └── (业务路由在 gateway.yaml.in / apps/observability 中加 RequestMirror)
│   └── observability/
│       ├── dashboards/waf.json
│       └── vmrule-waf.yaml
├── argocd/applications/
│   ├── waf.yaml
│   └── waf-console.yaml
├── scripts/
│   ├── waf-render.sh                 # site.yaml → *.conf
│   ├── waf-verify.sh                 # go-ftw 冒烟 + 日志可见性检查
│   └── waf-mirror.sh                 # 开关某站点镜像 / 全局关闭
├── docs/waf.md
└── .github/workflows/waf-lint.yml
```

Makefile 新增：

| 目标 | 作用 |
|---|---|
| `make waf-plan` | 渲染配置并做 `helm template` 和 diff，不改集群 |
| `make waf-verify` | 发送攻击样本，确认 VictoriaLogs 中出现事件、主链路状态码正常 |
| `make waf-mirror-off` | 紧急关闭所有站点的镜像（检测器异常或 Gateway 压力大时用） |
| `make waf-unblock IP=x.x.x.x` | 手动解封 |

---

## 5. 关键配置

### 5.1 HTTPRoute 镜像

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: gitea
  namespace: gitea
spec:
  parentRefs:
    - name: lab-gateway
      namespace: cilium-gateway
  hostnames: ["gitea.kbsonlong.com"]
  rules:
    - backendRefs:
        - name: gitea-http
          port: 3000
      filters:
        - type: RequestMirror
          requestMirror:
            backendRef:
              name: crs-detector
              namespace: waf
              port: 8080
            # percent: 100
```

跨命名空间引用需要 ReferenceGrant：

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-mirror-to-crs
  namespace: waf
spec:
  from:
    - {group: gateway.networking.k8s.io, kind: HTTPRoute, namespace: gitea}
    - {group: gateway.networking.k8s.io, kind: HTTPRoute, namespace: argocd}
    - {group: gateway.networking.k8s.io, kind: HTTPRoute, namespace: monitoring}
  to:
    - {group: "", kind: Service, name: crs-detector}
```

> 上线前需验证：Cilium 1.20 对跨命名空间 RequestMirror 和 `percent` 字段的支持情况。

### 5.2 检测器

```yaml
# helm/waf/values.yaml（节选）
detector:
  image: owasp/modsecurity-crs:nginx   # 固定到具体 tag，由 Renovate 升级
  env:
    MODSEC_RULE_ENGINE: DetectionOnly
    MODSEC_AUDIT_ENGINE: RelevantOnly
    MODSEC_AUDIT_LOG_FORMAT: JSON
    MODSEC_AUDIT_LOG_TYPE: Serial
    MODSEC_AUDIT_LOG: /dev/stdout
    MODSEC_RESP_BODY_ACCESS: "Off"
    MODSEC_REQ_BODY_LIMIT: "13107200"  # 12.5MiB，超出部分不检测
    BACKEND: http://127.0.0.1:8081     # 同 Pod 内的 dummy，返回 204
    PARANOIA: "2"                      # 旁路不影响业务，可多收集信号
    BLOCKING_PARANOIA: "1"
    ANOMALY_INBOUND: "5"
  priorityClassName: waf-low
```

注意事项：

1. **Host 后缀**：Envoy 会给镜像请求的 Host 加上 `-shadow` 后缀。nginx 用 `map $host $site { ~^(?<h>.+?)(-shadow)?$ $h; }` 把后缀去掉，再按站点加载规则。
2. **真实 IP**：配置 `set_real_ip_from <Gateway Pod CIDR>` 和 `real_ip_header X-Forwarded-For`，否则封禁对象会变成 Gateway 自己。
3. **无副作用**：上游是 dummy，镜像出来的 POST、PUT 不会被重复写入业务。
4. **大请求体**：Gitea 的 `*.git/git-receive-pack` 和 `git-upload-pack` 直接跳过检测（`ctl:ruleEngine=Off`）。

### 5.3 站点配置 `site.yaml`

```yaml
name: gitea
host: gitea.kbsonlong.com
mode: mirror            # mirror | inline | off
mirrorPercent: 100
paranoia: 2
anomaly: {inbound: 5, outbound: 4}
skipPaths:
  - '\.git/(git-upload-pack|git-receive-pack|info/refs)'
exclusions:
  - uri: /api/v1/repos/
    rules: [942100]
    target: ARGS_JSON
    reason: 代码片段被识别为 SQLi
```

`scripts/waf-render.sh` 把它渲染成 `before-crs.conf` 和 `after-crs.conf`。需要特殊处理时也可以直接手写 `.conf`，渲染脚本不会覆盖带 `# manual` 头的文件。

---

## 6. 联动封禁

### 6.1 触发规则（vmalert + LogsQL）

| 规则 | 条件 | 动作 |
|---|---|---|
| `WafHighAnomaly` | 同一 IP 5 分钟内异常评分累计 ≥ 25，或命中 ≥ 10 次 | 封 1h |
| `WafCriticalAttack` | 命中 930/932/942 中 CRITICAL 级别规则，或 913 扫描器特征 | 封 24h |
| `WafRepeatOffender` | 24 小时内被封 ≥ 3 次 | 封 7d |
| `WafDetectorDown` | 检测器不可用，或 5xx/超时比例 > 5% | 仅告警 |
| `WafSyncDegraded` | Argo CD `waf` 应用 Degraded | 仅告警 |

告警通过 webhook 发到 `waf-console /api/v1/alerts`，由控制台决定是否执行封禁，同时转发到 ntfy 主题 `waf`。

### 6.2 执行点：Gateway（Envoy）层，按 CF-Connecting-IP 封禁

外网链路是 Cloudflare Edge → cloudflared（VM100，`192.168.3.18`）→ Gateway VIP `192.168.3.243`（见 [gateway-access-log.md](gateway-access-log.md)）。
Gateway 在 L3 层看到的源地址永远是 tunnel，所以 **CIDR 和网络策略类封禁对外网流量无效**，只能在 L7 按 Cloudflare 透传的 `CF-Connecting-IP` 头匹配。
Cloudflare 自己的 WAF 规则数量有限，只用作补充。

```
Cloudflare ─► cloudflared ─► Cilium Gateway (Envoy)
                                ├─ HTTPRoute waf/waf-blocklist：Host ∈ 受保护站点 且 CF-Connecting-IP == 被封 IP ─► crs-detector:8082 ─► 403
                                └─ 业务 HTTPRoute（其他请求）─► 后端；同时 RequestMirror ─► crs-detector:8080
waf-console ── PUT ──► HTTPRoute waf/waf-blocklist（每个 IP 一个 match，过期时间存在 annotation 里）
```

- **只用 Gateway API 标准字段**：不写 CiliumEnvoyConfig，也不改 Cilium 生成的 listener，Cilium 升级时风险低。
- **优先级**：Gateway API 规定，Host 和路径相同时，header match 数量更多的规则优先。被封 IP 命中带 header 条件的封禁路由，其他请求继续走业务路由。上线前用 `make waf-verify-block` 实测确认。
- **容量**：每条规则最多 64 个 match，最多 16 条规则，所以名单上限 1024 个 IP（`BLOCK_MAX_ENTRIES` 默认 1000）。名单为空时保留一个永远不会命中的占位 match，避免路由因为没有任何 match 而匹配全部请求。
- **过期**：waf-console 每 30 秒清理一次过期条目。过期时间存在路由的 annotation 里，控制台重启不会丢失。
- **防伪造**：
  - 局域网客户端可以直接访问 VIP，并自己带上任意 `CF-Connecting-IP`。这样做只会让它自己被拒，影响不到别人。
  - 检测器侧，`trusted-proxies.conf` 只信任 cloudflared、节点和 Pod 网段，从 `X-Forwarded-For` 右侧取真实 IP。局域网客户端伪造的值会落在更左边，不会被采用，因此无法把别人送进封禁名单。
- **审计**：每次封禁响应都会在 crs-detector 输出一条 `waf_deny` JSON 日志，Gateway 访问日志也新增了 `cf_connecting_ip` 字段。
- **局限**：只对 `BLOCK_HOSTNAMES` 里的站点生效；局域网直连的请求不带 `CF-Connecting-IP`，所以不会被封。这两点都符合预期。

| 资源 | 管理方 |
|---|---|
| HTTPRoute `waf/waf-blocklist` | waf-console（运行时创建和更新，**不进 Git**，避免和 Argo CD 冲突） |
| crs-detector `:8082` | Argo CD 应用 `waf`（nginx 固定返回 403，并输出 `waf_deny` 日志） |
| Role `waf-console-blocker` | 只能操作 `waf` 命名空间里的 `waf-blocklist` 路由 |

### 6.3 安全阀

- `allowlist.yaml` 永不封禁：`192.168.3.0/24`、Tailscale 网段、Gateway/Pod CIDR、监控探针来源；
- 单次封禁的最长 TTL 为 7 天，同时封禁的 IP 数上限为 1000；
- 控制台提供“全部解封”和“暂停自动封禁”开关；
- 每次封禁或解封都追加记录到 `blocklist.yaml`（批量提交，最多 5 分钟一次），并发送 ntfy 通知。

---

## 7. 管理后台 waf-console

### 7.1 定位

无状态，Git 就是数据库。它对接四个外部系统：

```
浏览器 ──Gitea OAuth2──► waf-console
                          ├─ Git 适配层 (GitHub / Gitea API) ─ 改 config/waf，提 PR 或直接提交
                          ├─ LogsQL 客户端 ─ 查询 vlsingle
                          ├─ Argo CD API ─ 读取同步状态，触发 sync/rollback
                          └─ Blocker 客户端 ─ VM100 waf-blocker / Cilium 策略
```

访问方式：只在局域网通过 `waf.kbsonlong.com` 访问。控制台自身也挂镜像，但它不在任何拦截路径上。

### 7.2 页面

| 页面 | 功能 |
|---|---|
| 总览 | 各站点模式和镜像比例、24 小时命中和封禁数、检测器健康、当前 commit 和 Argo CD 状态 |
| 站点 | 新增站点（生成 `site.yaml` 和 HTTPRoute 镜像片段），切换 mirror/inline/off，调整 PL 和阈值 |
| 事件 | 按时间、站点、IP、规则过滤；查看单条请求的规则链、评分、请求详情（已脱敏） |
| 误报调优 | 在事件上点“标记误报”，生成 `ctl:ruleRemoveTargetById` 例外规则，预览 diff 后提 PR |
| 学习报告 | 按 规则 × URI × 参数 聚合命中，按“高频且来源分散”排序，作为误报候选 |
| 封禁中心 | 当前封禁、历史、手动封禁或解封、白名单、自动封禁阈值、暂停开关 |
| 规则 | 浏览 CRS 规则（按类别和 PL）、编辑自定义规则、语法预检 |
| 变更记录 | 列出 commit 和 PR，一键 revert |

### 7.3 API（节选）

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/v1/sites` | 站点列表及运行态 |
| PUT | `/api/v1/sites/{name}` | 更新站点并提交或提 PR |
| GET | `/api/v1/events?site=&ip=&rule=&from=&to=` | 查询事件 |
| POST | `/api/v1/events/{id}/false-positive` | 生成例外规则 PR |
| GET | `/api/v1/learning?site=&days=` | 学习报告 |
| GET/POST/DELETE | `/api/v1/blocks` | 封禁管理 |
| POST | `/api/v1/alerts` | vmalert webhook |
| GET | `/healthz`、`/metrics` | 探活与指标 |

### 7.4 变更流程

```
控制台操作 → 生成 diff → 推送分支并提 PR
  → GitHub Actions (waf-lint：yamllint、渲染、nginx -t、规则 ID 冲突检查)
  → 合并 → Argo CD auto-sync → ConfigMap hash 变化 → crs-detector 滚动重启
  → 控制台轮询同步结果 → ntfy 通知
```

单人使用时可以开启 `directCommit: true`，跳过 PR 直接提交到 main。封禁属于实时操作，不走这个流程，只把记录异步提交到 Git。

### 7.5 密钥

| Secret | 来源 | 用途 |
|---|---|---|
| `waf-console-git` | `.env` `WAF_GIT_TOKEN` | Git 读写 |
| `waf-console-oauth` | `.env` `WAF_OAUTH_CLIENT_ID/SECRET` | Gitea OAuth2 |
| `waf-console-argocd` | `.env` `WAF_ARGOCD_TOKEN` | 只读和 sync 权限的项目 Token |
| `waf-blocker` | `.env` `WAF_BLOCKER_TOKEN` | 调用 VM100 agent |
| `ntfy` | 复用现有 | 通知 |

---

## 8. 可观测性

配置在 `config/observability/waf/`，由 Argo CD 应用 `waf-observability` 管理，详细说明见该目录的 README。

```
crs-detector JSON ─► VictoriaLogs ─► vmalert(waf, LogsQL) ─► vmalertmanager(waf)
                                                               ├─ waf_action=block ─► waf-console ─► HTTPRoute waf-blocklist
                                                               └─ 其他告警 ─► ntfy 主题 waf
kube-state-metrics / cAdvisor / argocd / cilium-envoy ─► VictoriaMetrics ─► vmalert(waf, PromQL)
```

| 类别 | 告警 |
|---|---|
| 封禁 | WafHighAnomaly（≥10 次/5m，封 1h）、WafCriticalAttack（CRITICAL 级攻击规则，封 24h）、WafSensitiveProbe（敏感路径探测，封 6h） |
| 信号 | WafAttackSurge、WafNoEvents、WafDetectorErrors |
| 健康 | WafDetectorDown / Restarting / OOM / CPUThrottled、WafConsoleDown、WafArgoAppUnhealthy / OutOfSync、WafGatewayLatencyHigh |

Grafana 看板“WAF 旁路检测”分四块：
- **概览**：命中数、来源 IP、严重级别命中、检测器状态、正在触发的封禁告警；
- **趋势**：按站点、按攻击类别，以及 Gateway P99 延迟和检测器资源占用的对比；
- **Top N**：命中最多的规则（标出误报候选）、来源 IP、被攻击 URI、“规则 × URI × 参数”例外候选；
- **事件明细**：最近的检测事件。

所有 LogsQL 查询都依赖 VictoriaLogs 把审计日志的 JSON 展开成字段，上线后先执行 `make waf-obs-verify` 检查。

## 9. 局限与对策

| 局限 | 对策 |
|---|---|
| 第一个恶意请求会放行 | 对外高风险入口配合 Gateway 限流，或只允许通过 Tailscale 访问；必要时某站点改用 `mode: inline` |
| 无法检测响应内容 | 接受；敏感站点不对外暴露 |
| 检测器过载会漏检 | Envoy 镜像超时后丢弃，业务不受影响；设置 `WafDetectorDown` 告警，必要时降低 `mirrorPercent` |
| 误封 | 在 waf-console 里解封，或执行 `make waf-unblock IP=...` / `make waf-unblock IP=--all`（直接删除 waf-blocklist 路由） |
| 镜像让 Gateway 出口流量翻倍 | 在 homelab 规模下可以忽略；大文件路径可以不挂镜像（拆分 HTTPRoute rule） |

---

## 10. 部署步骤

前置检查：先排查 metrics-server、cilium-operator、vmagent 重启偏多的问题，避免资源紧张时影响 Gateway。

```bash
# 1. 填写 .env 中 WAF_* 变量
make check
make waf-plan

# 2. 部署检测器与控制台（Argo CD）
kubectl apply -f argocd/applications/waf.yaml
kubectl apply -f argocd/applications/waf-console.yaml

# 3. 逐个站点挂镜像（先 gitea）
#    在 HTTPRoute 中加 RequestMirror，提交后等 Argo CD 同步
make waf-verify SITE=gitea

# 4. 观察 3~7 天，补充例外规则后开启自动封禁
#    waf-console → 封禁中心 → 开启自动封禁

# 5. 下线旧实例
kubectl -n waf delete deploy crs-argocd crs-gitea crs-grafana
```

验收标准：

- [ ] 站点挂镜像前后，Gateway P99 延迟差异 < 5ms；
- [ ] go-ftw 攻击样本在 30 秒内出现在 VictoriaLogs；
- [ ] `make waf-verify-block` 全部通过：被封的 CF-Connecting-IP 返回 403，其他 IP 和局域网直连不受影响；TTL 到期后 30 秒内恢复；
- [ ] 局域网 IP 触发规则时不会被封；
- [ ] 停掉 `crs-detector` 后业务访问正常，并收到告警。

---

## 11. 回滚

| 场景 | 操作 |
|---|---|
| 检测器异常或影响 Gateway | `make waf-mirror-off`，或在 HTTPRoute 中删除 RequestMirror 后同步。注意：封禁路由的 403 也由 crs-detector:8082 返回，检测器停掉时被封 IP 会收到 503，仍然是拒绝 |
| 误封 | 在 waf-console 里解封，或执行 `make waf-unblock IP=...` / `make waf-unblock IP=--all`（直接删除 waf-blocklist 路由） |
| 规则配置错误 | `git revert` 后由 Argo CD 同步，或在 Argo CD 中 Rollback |
| 彻底移除 | 删除两个 Argo CD Application、HTTPRoute 镜像配置和 ReferenceGrant，再在 VM100 上删除 iptables 规则和 ipset |

---

## 12. 路线图

| 阶段 | 内容 |
|---|---|
| P0 | crs-detector、HTTPRoute 镜像、日志接入、`waf-verify` |
| P1 | 看板、VMRule、ntfy 告警、waf-lint CI |
| P2 | waf-console 只读版：总览、事件、学习报告 |
| P3 | 封禁中心与 VM100 waf-blocker，误报一键 PR |
| P4 | 混合模式（按站点 inline）、Renovate 跟踪 CRS 升级、GeoIP、威胁情报 |
