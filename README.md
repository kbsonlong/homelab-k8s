# homelab-k8s

可重复部署的家庭实验室 Kubernetes GitOps 基础设施。目标是把当前 Mac mini + Colima/k3s + PVE 节点上的实验室架构固化为 GitHub 管理的脚本和声明式配置。

## 架构摘要

```text
LAN 192.168.3.0/24
        |
Cilium LB-IPAM + L2 Announcement
        |
VIP 192.168.3.243  (唯一七层入口)
        |
Cilium Gateway API / Envoy
  +-- argocd.kbsonlong.com -> argocd-server ClusterIP
  +-- gitea.kbsonlong.com  -> gitea-http ClusterIP

VIP 192.168.3.241  (四层例外)
  +-- Gitea SSH :22 -> gitea-ssh LoadBalancer
```

原则：

- 七层 HTTP/HTTPS 服务统一挂到一个 Gateway，避免每个 Service 分配一个 LoadBalancer VIP。
- 只有 SSH、数据库、MQTT 等确实需要原始 TCP/UDP 的服务才使用独立 `LoadBalancer`。
- Cilium 已经承担 CNI、kube-proxy replacement、LB-IPAM、L2 announcement 和 Gateway API，不再额外部署 MetalLB。
- Helm 负责第三方应用版本，Kubernetes YAML 负责 Cilium/Gateway 声明式资源，脚本只负责编排和验证。

## 目录

- `docs/architecture.md`：拓扑、流量模型、VIP 规划和选型依据
- `docs/deployment.md`：从零开始的部署步骤、代理、密码和回滚
- `docs/operations.md`：日常运维、扩展 HTTPRoute、四层服务和故障排查
- `config/cilium/`：LB IP pool 与 L2 announcement policy
- `config/gateway/`：共享 Gateway 和 HTTPRoute 模板
- `helm/`：Argo CD、Gitea 的固定 values
- `scripts/`：幂等部署、检查、渲染、验证和受保护的销毁脚本

## 快速开始

```bash
git clone https://github.com/<your-account>/homelab-k8s.git
cd homelab-k8s
cp .env.example .env
$EDITOR .env

make check
make plan
make bootstrap
make verify
```

`make plan` 不修改集群；`make bootstrap` 才会执行 Helm upgrade/install 和 Kubernetes apply。真实密码只从 `.env` 或 CI secret 读取，禁止提交到 Git。

## 当前实验室版本基线

- Kubernetes：k3s `v1.36.5+k3s1`
- Cilium：`1.20.2`
- Gateway API CRD：`v1.5.0`
- Argo CD chart：`argo-cd-10.10.3`
- Gitea chart：OCI `docker.gitea.com/charts/gitea:12.7.0`
- Gitea：`1.27.0`
- StorageClass：`local-path`

版本集中在 `scripts/lib.sh`，升级时先改版本、运行 `make plan`，再在实验室执行 `make bootstrap`。

## GitHub 工作流

```bash
gh auth login
gh repo create homelab-k8s --public --source . --remote origin --push
```

仓库内置 GitHub Actions，对 shell、YAML、敏感信息和 Helm values 做静态校验。生产/家庭网络的真实凭据不进仓库。

## 安全边界

`make destroy` 是破坏性操作，必须显式执行 `CONFIRM=destroy make destroy`。它只删除本项目创建的 namespace 和 Cilium/Gateway 资源，不删除 k3s、Colima、PVE VM 或节点。
