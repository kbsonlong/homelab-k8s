# 10 使用本仓库部署 Cilium VIP 与应用

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

当至少一个节点为 `Ready` 后，回到仓库根目录：

```bash
cd /path/to/homelab-k8s
cp .env.example .env
$EDITOR .env
```

至少修改这些项目：

```dotenv
KUBE_CONTEXT=lab-default
BASE_DOMAIN=example.com
LB_POOL_START=192.168.3.240
LB_POOL_STOP=192.168.3.249
GITEA_ADMIN_USER=admin
GITEA_ADMIN_EMAIL=admin@example.com
GITEA_ADMIN_PASSWORD=
GITEA_DB_PASSWORD=
```

说明：

- `BASE_DOMAIN` 只影响 HTTPRoute 的 hostname；没有 DNS 时可以使用 `curl` 的 Host header 验证；
- `LB_POOL_START/STOP` 必须属于节点所在的二层网络；
- `config/cilium/lb.yaml.in` 当前把 L2 announcement 接口示例写为 `col0` 和 `eth0`。在 PVE、VMware、VirtualBox 或其他平台上，先用 `ip -br link` 确认实际 LAN 网卡，再按实际接口修改该文件；如果所有节点的接口名称不同，应统一调整后再运行 `make plan`；
- 密码只放在 `.env` 或 CI secret，`.env` 已被 `.gitignore` 排除；
- `rendered/` 是生成目录，不要把包含 Secret 的内容提交到 Git。

先做只读检查：

```bash
make check
make plan
```

`make plan` 不应创建应用资源；它会检查 context、Ready 节点、Helm template 和 server-side dry-run。

然后执行完整部署：

```bash
make bootstrap
```

首次完整部署的顺序是：

1. 安装 Gateway API CRD；
2. 升级 Cilium 的 Gateway API 和 L2 announcement 能力；
3. 应用 LB IP pool 与 L2 policy；
4. 安装/升级 Argo CD；
5. 创建 Gitea 所需 Secrets 并安装/升级 Gitea；
6. 创建共享 Gateway 和 Argo CD/Gitea HTTPRoute；
7. 等待 rollout 和状态检查；
8. 运行仓库验证脚本。
