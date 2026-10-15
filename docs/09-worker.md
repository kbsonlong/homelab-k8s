# 09 加入 worker 节点

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

如果只想做单节点实验，可以跳过本节。单节点适合学习，但节点故障会让所有应用同时不可用，`local-path` 数据也绑定在这一台节点上。

### 6.1 在 server 上取得 join token

在 server VM 上执行：

```bash
sudo cat /var/lib/rancher/k3s/server/node-token
```

这是敏感凭据，只在受控终端中使用，不要提交到仓库或粘贴到公共聊天。另记下 server 的固定 IP 和 API 端口 `6443`。

### 6.2 在 agent VM 上安装

先按第 3 节完成 agent 的系统初始化和网络检查，再执行：

```bash
export K3S_VERSION='v1.36.5+k3s1'
export SERVER_IP='192.168.3.83'
export K3S_TOKEN='<从 server 受控读取的 token>'

curl -sfL https://get.k3s.io | \
  K3S_URL="https://${SERVER_IP}:6443" \
  K3S_TOKEN="$K3S_TOKEN" \
  INSTALL_K3S_VERSION="$K3S_VERSION" \
  sh -s - agent \
  --node-ip='192.168.3.80'     # 替换成 agent 固定地址
```

检查：

```bash
sudo systemctl status k3s-agent --no-pager
sudo journalctl -u k3s-agent -n 100 --no-pager
kubectl get nodes -o wide
```

如果 agent 加入失败，按顺序检查：

1. agent 到 `<SERVER_IP>:6443` 是否可达；
2. server 和 agent 的 k3s 版本是否一致；
3. token 是否来自当前 server；
4. 主机名和 node name 是否重复；
5. 防火墙是否阻断了 k3s/Cilium 所需流量。
