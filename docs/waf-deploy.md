# WAF P0 部署包

按 homelab-k8s 仓库的目录结构组织，解压到仓库根目录即可（`Makefile.waf.mk` 需要在主 Makefile 里 `include`）。

## 文件

```
config/waf/                         # Argo CD 应用 "waf"
├── kustomization.yaml              # configMapGenerator：规则变更 → hash 变化 → 自动滚动更新
├── detector/                       # crs-detector：Deployment、Service(8080 检测 / 8082 封禁 403)、PriorityClass、CNP、nginx 模板
├── referencegrant.yaml             # 允许 argocd/gitea/monitoring 的 HTTPRoute 镜像到 crs-detector
├── sites/{argocd,gitea,grafana}/   # site.yaml + before-crs.conf
├── custom/rules.conf               # 自定义检测规则（ID 1100000+）
└── waf.yaml                        # 旧的串联 crs-*（未被 kustomization 引用，留作回滚）
config/gateway/gateway.yaml.in      # argocd-ui / gitea-http：直连后端 + RequestMirror；访问日志加 cf_connecting_ip
apps/observability/grafana-httproute.yaml   # grafana-ui：直连后端 + RequestMirror
config/observability/waf/           # vmalert、alertmanager、VMRule、Grafana 看板
apps/waf-console/                   # 控制台；deploy/ 含 RBAC、LAN 路由；运行时维护 HTTPRoute waf/waf-blocklist
argocd/applications/{waf,waf-console,waf-observability}.yaml
scripts/waf-{plan,verify,verify-gateway-block,mirror,secrets,unblock,observability-verify}.sh
```

## 部署前必须修改的 TODO

| 文件 | 内容 |
|---|---|

## 上线步骤

现状：3 条路由串联 `crs-*`（`backendRef` 指向 `crs-{argocd,gitea,grafana}.waf`）。本 PR 已经把它们改成**直连后端加 RequestMirror**，仓库里两个位置都改了：
- `config/gateway/gateway.yaml.in`：argocd-ui、gitea-http，由 `make render` 渲染，`make bootstrap` 或 `make waf-apply-routes` 应用；
- `apps/observability/grafana-httproute.yaml`：grafana-ui，由 Argo CD 应用 `observability` 同步（`helm/victoriametrics/grafana-httproute.yaml.in` 也同步改了）。

```bash
make waf-plan                                   # 1. 只读：kustomize 渲染、规则 ID 检查、kubectl diff
# 2. 合并并推送到 Gitea 后，部署检测器（这时还没有任何流量指向它）
kubectl apply -f argocd/applications/waf.yaml
kubectl -n waf rollout status deploy/crs-detector
kubectl -n waf logs deploy/crs-detector | grep -iE 'emerg|error' || echo ok

# 3. 切换路由：argocd、gitea 立即生效；grafana 在 Argo CD 同步 observability 后生效
make waf-apply-routes
make waf-verify                                 # 主链路正常、攻击样本能进 VictoriaLogs、client_ip 正确

# 4. 验证 Gateway 层封禁（用临时测试路由，不影响真实名单）
make waf-verify-block
#    再用手机热点访问一次 gitea，查看 Gateway 访问日志里的 cf_connecting_ip 是否是你的公网 IP

# 5. 可观测性和控制台
kubectl apply -f argocd/applications/waf-observability.yaml && make waf-obs-verify
make waf-secrets && kubectl apply -f argocd/applications/waf-console.yaml   # BLOCK_AUTO=false 观察 1～2 周

# 6. 观察几天、确认不再需要回滚到串联后，下线旧实例：删除 config/waf/waf.yaml 里的 crs-* 段
```

> `config/waf/waf.yaml`（旧的 3 个 crs-* 和 waf Namespace）没有被新的 kustomization 引用，作为串联模式的回滚手段先保留。确认完全不需要后再删除。如果它目前是 `kubectl apply` 部署的，保持不动即可。

## 回滚

| 场景 | 操作 |
|---|---|
| 怀疑镜像影响 Gateway | `make waf-mirror-off` |
| 检测器异常 | `kubectl -n waf scale deploy crs-detector --replicas=0`（镜像目标没有可用后端，主链路不受影响） |
| 误封 | `make waf-unblock IP=x.x.x.x`；全部清空：`make waf-unblock IP=--all` |
| 回到串联模式 | 3 条路由的 backendRefs 改回 `crs-*.waf:80` 并删除 RequestMirror（文件里已写注释） |
| 整体移除 | `make waf-mirror-off`，然后删除 waf / waf-console / waf-observability 三个 Argo CD 应用 |

## 需要在集群上确认的假设

1. 镜像里的 `/etc/modsecurity.d/setup.conf` 和 `/etc/nginx/templates/conf.d/default.conf.template` 路径和当前 tag 一致；镜像的 nginx.conf 没有在 http 层重复开启 modsecurity（用 `kubectl exec ... nginx -T` 检查）。
2. libmodsecurity v3 的 `Include` 支持通配符 `*-before-crs.conf`。CI 里的 `nginx -t` 能发现这个问题。
3. Cilium 1.20 支持跨命名空间的 RequestMirror，镜像请求的 Host 带 `-shadow` 后缀。
4. 审计日志里的 `transaction.client_ip` 是真实客户端 IP（`waf-verify.sh` 第 4 步会检查）。
