# 08 安装 Cilium 并使节点 Ready

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

### 5.1 先处理“新集群 bootstrap”这一特殊阶段

本仓库的 `make check` 要求至少有一个 `Ready` 节点，而禁用 Flannel 的新 k3s 在 Cilium 安装前必然是 `NotReady`。因此第一次安装必须先手动种下 Cilium；节点变为 `Ready` 后，再交给仓库脚本继续完成 Gateway、Argo CD 和 Gitea。

在部署机上，确认 `kubectl` 指向新 server：

```bash
kubectl config current-context
kubectl get nodes -o wide
```

安装 Helm 仓库并先安装 Cilium。命令中的版本应与仓库 `scripts/lib.sh` 的 `CILIUM_VERSION` 一致：

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update cilium

helm upgrade --install cilium cilium/cilium \
  --version 1.20.2 \
  --namespace kube-system \
  --reuse-values \
  --set kubeProxyReplacement=true \
  --set gatewayAPI.enabled=true \
  --set l2announcements.enabled=true \
  --wait \
  --timeout 10m
```

如果目标 Cilium 版本对 `kubeProxyReplacement` 的配置方式不同，优先以该版本的官方 chart 文档和 `helm show values cilium/cilium --version <版本>` 为准；不要盲目复制其他版本的参数。

### 5.2 Cilium 检查点

```bash
kubectl -n kube-system get pods -l k8s-app=cilium -o wide
kubectl -n kube-system get pods -l name=cilium-operator
kubectl get nodes -o wide
kubectl -n kube-system exec ds/cilium -- cilium-dbg status
```

预期：

- Cilium agent 和 operator 为 `Running`；
- node 变为 `Ready`；
- `cilium-dbg status` 没有明显的 connectivity error；
- Cilium 接管 Service 转发，而不是 k3s kube-proxy。

如果节点仍是 `NotReady`：

```bash
kubectl describe node <node-name>
kubectl -n kube-system get pods -o wide
kubectl -n kube-system logs ds/cilium --all-containers --tail=100
sudo journalctl -u k3s -n 100 --no-pager
```

先解决 CNI 错误，再继续。不要直接删除 k3s 数据目录重装。
