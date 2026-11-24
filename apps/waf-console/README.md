# waf-console

homelab-k8s 旁路 WAF 的管理后台，设计见 `docs/waf.md`。

- 无状态：配置存 Git（`config/waf/`），日志查 VictoriaLogs，同步状态读 Argo CD
- 封禁：在 Cilium Gateway（Envoy）层按 `CF-Connecting-IP` 拒绝。waf-console 维护 HTTPRoute `waf/waf-blocklist`，被封 IP 的请求转到 crs-detector:8082 返回 403。审计记录批量写回 Git
- 认证：前置 oauth2-proxy（Gitea OIDC）；vmalert webhook 使用 `ALERT_TOKEN`

## 目录

```
cmd/waf-console/      主程序
cmd/waf-blocker/      VM100 ipset agent
internal/api/         HTTP 路由、处理器、内嵌前端（Alpine.js 单页）
internal/gitops/      Gitea API（读写文件、多文件提交、PR、提交历史）
internal/logs/        VictoriaLogs LogsQL 客户端、事件解析、学习报告
internal/argocd/      Argo CD 状态与同步
internal/blocker/     封禁管理（白名单、TTL、上限、重复升级、Git 审计）
internal/waf/         站点模型、校验、before-crs.conf 渲染
deploy/               Deployment(+oauth2-proxy)、VMRule 示例
```

## 本地运行

```bash
make tidy && make test
GIT_TOKEN=xxx GIT_URL=https://gitea.kbsonlong.com \
VLOGS_URL=http://localhost:9428 ARGOCD_URL=https://localhost:8080 ARGOCD_TOKEN=xxx \
go run ./cmd/waf-console
# 绕过 oauth2-proxy 本地调试：
curl -H 'X-Forwarded-User: me' localhost:8080/api/v1/overview
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GIT_URL/OWNER/REPO/BRANCH` | gitea.kbsonlong.com / kbsonlong / homelab-k8s / main | 配置仓库 |
| `GIT_TOKEN` | 必填 | Gitea token（repo 读写） |
| `GIT_DIRECT_COMMIT` | true | false 时走分支 + PR |
| `VLOGS_URL` | vlsingle 集群地址 | VictoriaLogs |
| `ARGOCD_URL/TOKEN/APP` | — / — / waf | Argo CD |
| `BLOCK_BACKEND` | gateway | gateway / ipset |
| `BLOCK_HEADER` | CF-Connecting-IP | 真实客户端 IP 头 |
| `BLOCK_HOSTNAMES` | gitea/argocd/grafana.kbsonlong.com | 受保护站点（逗号分隔） |
| `BLOCK_GATEWAY` / `_NAMESPACE` / `_SECTION` | lab-gateway / cilium-gateway / http | 封禁路由挂载的 Gateway |
| `BLOCK_DENY_SERVICE` / `_PORT` | crs-detector / 8082 | 403 响应服务 |
| `BLOCKER_URL/TOKEN` | — | ipset 模式下的 agent |
| `BLOCK_AUTO` | false | 自动封禁初始状态 |
| `BLOCK_DEFAULT_TTL/MAX_TTL/MAX_ENTRIES` | 1h / 168h / 1000 | 安全阀 |
| `ALERT_TOKEN` | — | vmalert webhook 鉴权 |
| `NTFY_URL` | http://ntfy.ntfy.svc/waf | 通知 |

## API

| Method | Path | 说明 |
|---|---|---|
| GET | /api/v1/overview | 总览 |
| GET/PUT | /api/v1/sites[/{name}] | 站点读取/更新（提交到 Git） |
| GET | /api/v1/events?host=&ip=&rule=&from=&to=&limit= | 事件 |
| POST | /api/v1/events/false-positive | 误报 → 例外规则（`apply:false` 预览） |
| GET | /api/v1/learning?from=&to= | 学习报告 |
| GET/POST | /api/v1/blocks | 封禁列表/手动封禁 |
| DELETE | /api/v1/blocks/{ip} | 解封 |
| PUT | /api/v1/blocks/auto | 开关自动封禁 |
| POST | /api/v1/alerts | vmalert webhook |
| GET | /api/v1/changes | 配置变更历史 |
| POST | /api/v1/sync | 触发 Argo CD 同步 |

## TODO

- [ ] GitHub 适配（`gitops.Repo` 第二实现）
- [ ] 验证 Cilium 对“header match 更多的路由优先”的实现（make waf-verify-block）
- [ ] 站点新增时同时生成 HTTPRoute RequestMirror 片段
- [ ] 前端规则浏览器（CRS 规则索引）与自定义规则编辑
- [ ] 封禁元数据持久化（当前重启后仅保留 ipset 中的 IP/TTL）
- [ ] 核对审计日志 JSON 字段与 LogsQL 查询
