# 01：从创建 VM 到 k3s 集群与实验室应用

这是本仓库面向新人的分章式入门教程。每个章节都是独立 Markdown 文件，后续更新某一阶段时只需读取对应文件。

目标链路：

1. 规划硬件、网络、节点和 VIP；
2. 创建 Linux VM（PVE `qm`、cloud-init、Colima bridged 等路径）；
3. 初始化系统并安装 k3s server/agent；
4. 安装 Cilium CNI、Gateway API 和局域网 LoadBalancer；
5. 使用本仓库部署 Argo CD、Gitea 和共享 Gateway；
6. 按检查点验证、排障、回滚和销毁。

> 默认命令以 Ubuntu 24.04 LTS、k3s、Cilium 和 Linux/macOS 部署机为例。硬件、虚拟化平台、网段和域名可以替换；示例地址不是必须值。

## 按顺序阅读

| 编号 | 章节 | 适用场景 |
|---:|---|---|
| 01 | [总览：最终形态与组件边界](01-overview.md) | 先理解最终要搭什么 |
| 02 | [准备：硬件、软件与网络规划](02-prerequisites.md) | 创建资源前做规划 |
| 03 | [创建 Linux VM：通用要求与 PVE 图形界面](03-vm-options.md) | 手工安装系统或先熟悉 PVE |
| 04 | [PVE `qm`、raw 镜像与 cloud-init 模板](04-pve-qm-cloud-init.md) | 自动下载镜像、克隆和初始化 VM |
| 05 | [Colima bridged networking](05-colima-bridge.md) | macOS 上创建可达 LAN 的 VM |
| 06 | [初始化 Linux 操作系统](06-os-init.md) | 每台 k3s 节点执行 |
| 07 | [安装第一个 k3s server](07-k3s-server.md) | 创建控制面和 kubeconfig |
| 08 | [安装 Cilium 并使节点 Ready](08-cilium.md) | 新集群首次 CNI bootstrap |
| 09 | [加入 worker 节点](09-worker.md) | 扩展计算节点 |
| 10 | [使用本仓库部署 Cilium VIP 与应用](10-repo-deploy.md) | Argo CD、Gitea、Gateway |
| 11 | [部署后检查与访问验证](11-verify.md) | 检查点与验收 |
| 12 | [添加自己的应用](12-app.md) | HTTPRoute 与 ClusterIP |
| 13 | [回滚、排障与销毁](13-operations.md) | 日常故障和破坏性操作 |
| 14 | [下一步学习顺序](14-next.md) | 完成基础安装之后 |
| 15 | [已知边界](15-boundaries.md) | 生产化前必须理解的限制 |

## 两条创建 VM 路径

- **PVE `qm` + Ubuntu cloud image**：适合长期运行的 server/agent，使用固定 VM ID、VirtIO、cloud-init 和模板克隆。
- **Colima bridged**：适合 macOS 上的实验节点；需要 `socket_vmnet`、正确的 `network.address/mode/interface`，并且不要和 Colima 自带 Kubernetes 重复安装 k3s。

两条路径最后都汇合到第 06 章：系统初始化 → 第 07 章：k3s server → 第 08 章：Cilium → 第 10 章：仓库应用部署。

## 仓库入口

已有可用 kubeconfig 和 `Ready` 节点时，可直接阅读 [`docs/deployment.md`](deployment.md)。
