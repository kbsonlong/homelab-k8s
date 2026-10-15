# 已有 k3s 集群的应用部署

本文适用于已经拥有可用 kubeconfig 和至少一个 `Ready` 节点的集群。若需要从创建 VM、安装 k3s 开始，请先阅读 [`01-from-vm-to-k3s.md`](01-from-vm-to-k3s.md)。

本机需要：`kubectl`、`helm`、`curl`、`python3`、`git`、`bash`。部署机需要已经拥有目标集群 kubeconfig；本仓库不会生成或上传 kubeconfig。

当前实验室默认 context 是 `lab-default`。外网下载需要家庭代理：

```bash
export HTTPS_PROXY=http://192.168.3.16:1087
export HTTP_PROXY=http://192.168.3.16:1087
```

## 2. 配置秘密

```bash
cp .env.example .env
$EDITOR .env
```

至少填写：

- `GITEA_ADMIN_PASSWORD`
- `GITEA_DB_PASSWORD`
- `BASE_DOMAIN`

The deployment creates `gitea-admin-auth` and `gitea-postgresql-auth` in the target namespace from these environment variables, then references those Secrets from Helm. Passwords are not passed as Helm values.

`.env` 已被 `.gitignore` 排除。不要把密码写进 `helm/` values 或 shell 历史。

## 3. 只读检查与计划

```bash
make check
make plan
```

`make plan` 会：

- 校验工具和 Kubernetes context；
- 检查 Helm chart 能否解析；
- 对 Cilium/Gateway 配置执行 server-side dry-run；
- 对应用 Helm release 执行 template/render；
- 不创建 namespace、Service、Pod 或 PVC。

## 4. 一键部署

```bash
make bootstrap
```

编排顺序：

1. 安装 Gateway API `v1.5.0` CRD；
2. 升级 Cilium Gateway API 和 L2 announcement 开关；
3. 重启 Cilium agent/operator，避免 ConfigMap 改完但 agent 未 reload；
4. 应用 LB IP pool 和 L2 policy；
5. 安装/升级 Argo CD，Web Service 使用 `ClusterIP`；
6. 安装/升级 Gitea，HTTP 使用 `ClusterIP`、SSH 使用 `LoadBalancer`；
7. 创建共享 Gateway 和两个 HTTPRoute；
8. 等待 rollout 和 Gateway 状态；
9. 执行验证。

脚本是幂等的，可以重复执行。Helm chart 版本固定在 `scripts/lib.sh`。

## 5. 验证

```bash
make status
make verify
```

预期：

- Gateway `Accepted=True`、`Programmed=True`；
- Gateway 从池中获取一个 VIP；
- Gitea HTTP Route 返回 200；
- Argo CD Route 返回 200 或其配置的 HTTPS redirect；
- `gitea-ssh` 是唯一业务 L4 LoadBalancer；
- `argocd-server` 和 `gitea-http` 是 ClusterIP。

本地未配置 DNS 时，验证脚本使用 `curl --resolve`/Host header，不依赖公网 DNS。

## 6. 回滚

应用回滚优先使用 Helm：

```bash
helm history argo-cd -n argocd
helm rollback argo-cd <REVISION> -n argocd
helm history gitea -n gitea
helm rollback gitea <REVISION> -n gitea
```

Gateway 路由回滚：

```bash
kubectl delete -f rendered/gateway.yaml
```

如果需要临时直连调试，可手动把 HTTP Service 改成 LoadBalancer；调试完成后应恢复为 ClusterIP，避免破坏 VIP 规划。

## 7. 销毁边界

```bash
CONFIRM=destroy make destroy
```

脚本会再次打印将删除的 namespace/resource，并只删除本项目资源。它不会删除节点、Colima、PVE VM 或 Cilium Helm release。生产数据 PVC 不应通过本脚本删除。
