# 01 总览：最终形态与组件边界

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

最小可行拓扑是一台 VM 同时运行 k3s server 和工作负载；推荐入门拓扑是一个 server 加一个 agent；需要控制面高可用时再增加 server，并采用 k3s embedded etcd 或外部数据库方案。

```text
                    管理电脑
              kubectl / helm / git
                         |
                 LAN / 管理网络
                         |
       +-----------------+------------------+
       |                                    |
   k3s-server VM                        k3s-agent VM
   API: 6443                            运行工作负载
   Cilium                        （可选，可增加多个）
       |                                    |
       +-----------------+------------------+
                         |
              Cilium L2 + LB-IPAM
                         |
     Gateway VIP（HTTP/HTTPS）  独立 L4 VIP（SSH/TCP）
                         |
          Argo CD / Gitea / 其他应用
```

本仓库当前的应用链路是：

- Cilium：CNI、kube-proxy replacement、LoadBalancer IPAM、L2 announcement、Gateway API/Envoy；
- Gateway API：共享七层入口，普通 HTTP 服务不再各自申请一个 VIP；
- Argo CD：GitOps 控制面；
- Gitea：Git 服务，HTTP 走共享 Gateway，SSH 保留独立 `LoadBalancer`；
- `local-path`：简单的节点本地持久化，不是高可用存储。
