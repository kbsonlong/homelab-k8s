# 14 下一步学习顺序

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

完成本文后，建议按以下顺序扩展，而不是一次安装所有组件：

1. 给 Gitea 和 Argo CD 配置可靠 DNS/TLS；
2. 为重要 PVC 建立定期备份和恢复演练；
3. 增加 Prometheus/Grafana，并设置资源上限；
4. 为应用补充 CiliumNetworkPolicy；
5. 把一个示例应用接入 Argo CD，验证 Git push 到集群的完整 GitOps 流程；
6. 再研究多 server HA、外部数据库、Longhorn/Ceph 等复杂能力。

不要把 `local-path`、单 server、单块虚拟磁盘和无备份的快照误认为生产级高可用方案。
