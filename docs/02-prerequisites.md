# 02 准备：硬件、软件与网络规划

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

### 1.1 硬件建议

硬件不必和作者的实验室相同，先按工作负载选择规模：

| 场景 | server 节点 | agent 节点 | 适合用途 |
|---|---:|---:|---|
| 单 VM 入门 | 2 vCPU / 4 GiB / 40 GiB | 无 | 学习、验证脚本、低负载服务 |
| 推荐实验室 | 2–4 vCPU / 4–8 GiB / 60 GiB | 每台 2–4 vCPU / 4–8 GiB / 60 GiB | Argo CD、Gitea、监控和常规开发 |
| 多服务实验室 | 4–8 vCPU / 8–16 GiB / 100 GiB | 按需增加 | 数据库、CI runner、日志和多个应用 |
| 控制面高可用 | 至少 3 台 server | 0 或多台 | 需要 quorum、备份和更严格运维 |

最低配置只是“能启动”，不是性能承诺。数据库、镜像仓库、监控和日志会明显增加内存与磁盘需求。优先使用 SSD；不要把重要数据只放在临时盘或单个 `local-path` 节点上。

作者当前实验室的一个示例是：Mac mini/Colima 运行一个节点，PVE 上运行一个 worker，LAN 为 `192.168.3.0/24`，网关为 `192.168.3.1`。这些值只用于帮助理解，不能直接复制到其他网络。

### 1.2 部署机需要的软件

部署机可以是 macOS、Linux 或 Windows + WSL2。安装以下工具，并确认版本：

```bash
kubectl version --client
helm version
ssh -V
git --version
curl --version
python3 --version
```

本仓库的 CI 还会使用 `shellcheck`、`yamllint` 和 `python3-yaml`。它们用于检查仓库，不要求安装在 k3s 节点上。

### 1.3 先画出网络规划

在创建 VM 前先确定这些值：

```text
LAN_CIDR       = 192.168.3.0/24       # 示例，替换成你的 LAN
LAN_GATEWAY    = 192.168.3.1          # 示例
DNS_SERVER     = 192.168.3.1          # 或你的 DNS 服务
SERVER_IP     = 192.168.3.83          # 固定 DHCP lease 或静态地址
AGENT_IP      = 192.168.3.80          # 可选
LB_POOL       = 192.168.3.240-249     # 必须是未被 DHCP/主机占用的地址
BASE_DOMAIN   = kbsonlong.com         # 替换成自己的域名
```

规则：

- k3s 节点之间必须能互通；
- 部署机必须能访问 server 的 TCP `6443`；
- 节点需要访问 DNS、软件仓库、容器镜像仓库；
- `LB_POOL` 必须和节点处于可达的二层网络，且不能和 DHCP 地址池重叠；
- 生产环境不要把 API `6443` 暴露到公网；
- 如果使用 Cloudflare Tunnel，公网 TLS 可以在隧道侧终止，集群内部仍可使用 HTTP Gateway；
- 如果使用 HTTPS Gateway，则需要额外配置 TLS Secret 和证书管理，不要把私钥提交到 Git。

在路由器或 DHCP 服务中为每个 VM 建议使用固定 DHCP lease。固定地址比在 VM 内手写静态 IP 更容易迁移和排障；两者只能选一种，不要重复配置。
