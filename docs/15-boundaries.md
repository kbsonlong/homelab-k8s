# 15 已知边界

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

- 本文没有自动创建 PVE VM，也不会替你修改路由器、DHCP、DNS、Cloudflare Tunnel 或公网防火墙；这些属于环境级操作；
- Cilium、k3s 和 Gateway API 版本应与 `scripts/lib.sh`、`.env` 和 CI 保持一致；升级前先运行 `make plan`；
- 本仓库当前脚本假设 Cilium 已经让至少一个节点进入 `Ready`，所以新集群首次安装必须按第 5 节手动预装 Cilium；
- `local-path` 不提供跨节点复制；节点损坏时，未备份的数据可能丢失；
- 只有完成真实网络、DNS、TLS、备份、恢复和故障演练后，才能评估是否满足生产需求。
