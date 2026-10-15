# 实验室架构方案

## 1. 目标

实验室需要同时满足：

1. 服务可通过 GitHub 中的声明式文件重建。
2. HTTP/HTTPS 服务共享一个七层入口，节约 LAN VIP。
3. SSH、数据库、消息队列等四层协议保留独立 LoadBalancer。
4. CNI、网络策略、负载均衡和 Gateway 尽量由同一套组件提供，减少运维面。
5. 部署操作可审计、可回滚，不把密码和 kubeconfig 放进仓库。

## 2. 物理与虚拟拓扑

- Mac mini：运行 Colima/k3s control-plane，LAN `192.168.3.83`。
- PVE 节点 `lab-node`：k3s worker，LAN `192.168.3.80`。
- 家庭网段：`192.168.3.0/24`，网关 `192.168.3.1`。
- VM100 `192.168.3.16`：边界代理和 Cloudflare Tunnel，属于外部入口层，不由本仓库重启或重配。

## 3. Kubernetes 网络

Cilium `1.20.2` 已经作为 CNI 并启用 kube-proxy replacement。LB-IPAM 从 `192.168.3.240-249` 分配 VIP，L2 announcement 通过 `col0`/`eth0` 回答 LAN ARP。Cilium Gateway API 使用其内置 Envoy 数据面。

这比额外安装 MetalLB 更适合当前实验室：已经有 Cilium，没有额外 speaker/controller；代价是 LB/Gateway 能力与 Cilium 绑定。

## 4. VIP 规划

| 用途 | 地址 | 类型 | 说明 |
|---|---:|---|---|
| Shared Gateway | `192.168.3.243` | L7 | 所有 HTTP/HTTPS hostname 路由 |
| Gitea SSH | `192.168.3.241` | L4 | TCP/22，保留独立 LoadBalancer |
| 保留池 | `.240-.249` | 预留 | 仅特殊 L4 或未来 Gateway |

迁移后，Argo CD HTTP 和 Gitea HTTP Service 必须是 `ClusterIP`；它们只允许由 Gateway 在集群内访问。Gitea SSH Service 保持 `LoadBalancer`。

## 5. 流量模型

```text
Client -> 192.168.3.243:80/443 -> Cilium Envoy
  Host: argocd.example -> argocd-server:80
  Host: gitea.example  -> gitea-http:80

Git client -> 192.168.3.241:22 -> gitea-ssh:22
```

TLS 有两种部署方式：

- Cloudflare Tunnel 在 VM100 终止公网 TLS，回源到 Gateway HTTP；适合当前实验室。
- 在 Gateway 增加 HTTPS listener 和 Kubernetes TLS Secret；建议配合 cert-manager，证书不由本仓库硬编码。

## 6. 组件边界

- Cilium：网络、Service LB、L2、Gateway API/Envoy。
- Gateway API：标准化 `GatewayClass`、`Gateway`、`HTTPRoute` 资源。
- Argo CD：GitOps 控制面；其 Web UI 作为 HTTPRoute backend。
- Gitea：Git server；Web 作为 HTTPRoute backend，SSH 作为 L4 backend。
- local-path：实验室单节点/低复杂度持久化，不等价于高可用存储。

## 7. 非目标

本仓库不自动创建 PVE VM、不重装 k3s、不修改 VM100、不配置 Cloudflare Tunnel、不把公开 GitHub 仓库当作私密配置存储。节点创建和隧道配置有意保留为环境级操作。
