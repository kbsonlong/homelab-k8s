# 06 初始化 Linux 操作系统

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

在每台节点上执行。`<user>`、`<SERVER_IP>` 等占位符必须替换成自己的值。

```bash
sudo apt-get update
sudo apt-get full-upgrade -y
sudo apt-get install -y curl ca-certificates jq open-iscsi nfs-common
sudo systemctl enable --now iscsid 2>/dev/null || true
```

确认主机名唯一：

```bash
sudo hostnamectl set-hostname k3s-server-01   # server 示例
# worker 上改为 k3s-agent-01
hostname
```

k3s 通常不要求关闭 swap，但为了减少不同发行版和不同 kubelet 行为带来的变量，入门实验统一关闭：

```bash
sudo swapoff -a
sudo sed -i.bak '/\sswap\s/s/^/#/' /etc/fstab
```

加载常用内核模块并设置网络参数：

```bash
sudo tee /etc/modules-load.d/k3s.conf >/dev/null <<'EOF'
br_netfilter
overlay
EOF
sudo modprobe br_netfilter
overlay

sudo tee /etc/sysctl.d/99-k3s.conf >/dev/null <<'EOF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
sudo sysctl --system
```

如果主机启用了 UFW 或其他防火墙，至少按你的拓扑放通：

- server API：TCP `6443`，只允许管理网和 agent；
- 节点间的 k3s/CNI 流量：按所选 Cilium 模式开放；
- VXLAN/Geneve/WireGuard 等 Cilium 所需流量：以当前 Cilium 文档和实际模式为准；
- 不要为了省事把所有端口暴露到公网。

检查结果：

```bash
swapon --show       # 应无输出
sysctl net.ipv4.ip_forward
lsmod | grep -E 'br_netfilter|overlay'
```
