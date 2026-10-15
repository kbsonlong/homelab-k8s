# 07 安装第一个 k3s server

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

### 4.1 选择网络模式

本仓库使用 Cilium，不使用 k3s 自带 Flannel、Traefik 和 ServiceLB。因此 server 安装时禁用这些组件，并禁用 k3s 自带 kube-proxy，让后续 Cilium 接管服务转发。

这一步会导致节点在 CNI 安装前显示 `NotReady`，这是预期现象；不要在 CNI 未安装时把它误判为安装失败。

先在 server VM 上设置版本变量。版本应在团队内固定，不要每次安装自动追最新版本：

```bash
export K3S_VERSION='v1.36.5+k3s1'   # 示例；升级前先确认仓库兼容性
export SERVER_IP='192.168.3.83'      # 替换成 server 的固定地址
```

执行安装：

```bash
curl -sfL https://get.k3s.io | \
  INSTALL_K3S_VERSION="$K3S_VERSION" \
  sh -s - server \
  --node-ip="$SERVER_IP" \
  --advertise-address="$SERVER_IP" \
  --tls-san="$SERVER_IP" \
  --flannel-backend=none \
  --disable-network-policy \
  --disable-kube-proxy \
  --disable=traefik \
  --disable=servicelb
```

安装完成后检查服务和 API：

```bash
sudo systemctl status k3s --no-pager
sudo journalctl -u k3s -n 100 --no-pager
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl get pods -A
```

预期：API 可以响应，但 node 可能是 `NotReady`；`coredns`、`local-path-provisioner` 等 Pod 可能等待网络。

### 4.2 导出 kubeconfig 到部署机

不要把 `/etc/rancher/k3s/k3s.yaml` 提交到 Git，也不要把其中的 client key 发到聊天工具。用 SSH 复制到部署机后，仅修改 server 地址：

```bash
mkdir -p "$HOME/.kube"
ssh <user>@<SERVER_IP> 'sudo cat /etc/rancher/k3s/k3s.yaml' \
  | sed "s/127.0.0.1/<SERVER_IP>/g" \
  > "$HOME/.kube/k3s-lab.yaml"
chmod 600 "$HOME/.kube/k3s-lab.yaml"

export KUBECONFIG="$HOME/.kube/k3s-lab.yaml"
kubectl get nodes -o wide
kubectl cluster-info
```

如果本机已经有其他 kubeconfig，不要直接覆盖；可以使用 `KUBECONFIG=file1:file2 kubectl config view --flatten` 合并，并为当前 context 取一个明确名称：

```bash
kubectl config rename-context default lab-default
kubectl config current-context
```

本仓库默认使用 `lab-default`。如果你使用其他名称，在 `.env` 中设置：

```bash
KUBE_CONTEXT=你的-context
```
