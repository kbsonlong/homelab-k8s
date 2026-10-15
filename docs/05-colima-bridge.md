# 05 Colima：macOS 创建可达 LAN 的 bridged VM

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

本章说明在 macOS 上使用 Colima 创建一个通过真实 LAN 获取 DHCP 地址的 VM。该 VM 可以被 PVE 和同一 LAN 的其他设备访问，适合把 Mac mini 作为 k3s server 或 worker。

> 本章只创建 Colima VM，不启用 Colima 自带 Kubernetes。为了和本仓库的 Cilium 方案一致，k3s 会在 Colima VM 内按 [07 k3s server](07-k3s-server.md) 或 [09 worker](09-worker.md) 单独安装。

## 5.1 适用条件和限制

- macOS 13+，Apple Silicon 或 Intel 均需确认 Colima/VM type 支持；
- Colima profile 使用 `vz` 或当前平台支持的 VM type；
- Mac 的物理网络必须允许 vmnet bridged；某些 Wi-Fi、企业网络和 VPN 会阻止二层桥接；
- 需要 `socket_vmnet` 和一次 root 权限配置；
- bridged VM 会从真实 LAN DHCP 获取地址，不使用 Colima 默认的 `192.168.5.0/24` shared NAT；
- 本仓库的 Cilium L2 配置示例包含 `col0`，PVE guest 通常是 `eth0`，必须按每个节点实际接口配置；
- Colima 自带 `--kubernetes` 是另一套 k3s，不要和本仓库手工安装的 k3s 混用。

## 5.2 安装并确认工具

在 Mac 主机执行：

```bash
brew install colima lima socket_vmnet
colima version
limactl --version
```

本仓库当前验证过 Colima `0.10.3`。版本变化后先查看实际帮助：

```bash
colima start --help | grep -E 'network|profile|vm-type|runtime'
colima template --print
```

确认物理 LAN 接口。不要直接假定是 `en0`：

```bash
route get 192.168.3.1 | awk '/interface:/{print $2}'
networksetup -listallhardwareports
```

把输出中的真实接口填入下面的 `HOST_IF`。当前示例实验室使用 `en1`，但其他 Mac、USB 网卡或有线网络可能不同。

## 5.3 准备 socket_vmnet 和 sudoers

先检查 Colima 的 root-owned helper：

```bash
ls -l /opt/colima/bin/socket_vmnet \
  /opt/homebrew/opt/socket_vmnet/bin/socket_vmnet 2>/dev/null || true
```

Colima bridged 的 helper 必须能以 root 运行。优先使用 Colima 当前版本随附/安装的 `/opt/colima/bin/socket_vmnet`；不要把普通用户可写目录里的同名二进制授予 root。

首次运行建议使用交互式终端，让 Colima 安装或更新与当前版本匹配的 sudoers 规则：

```bash
export HOST_IF=en1       # 替换成上一步确认的接口
colima start --profile k3s \
  --vm-type vz \
  --runtime containerd \
  --network-address \
  --network-mode bridged \
  --network-interface "$HOST_IF" \
  --cpu 4 \
  --memory 8 \
  --disk 60 \
  --save-config
```

如果出现 sudo 密码提示，应在本机交互式终端完成。Agent、CI 或无 TTY 环境无法代填 sudo 密码；不要把密码写进脚本、命令行或聊天。

验证 sudoers：

```bash
sudo -n true
sudo visudo -c
ls -l /etc/sudoers.d/colima 2>/dev/null || true
```

Colima 版本升级后，旧 sudoers 内容可能缺少新模板行；文件权限过严导致当前用户不可读时，也可能被 Colima 判断为未安装。遇到反复要求 sudo、`vmnet socket file not found` 或启动后自动退回 shared NAT 时：

1. 用当前安装的 Colima 版本重新执行上面的交互式 `colima start`；
2. 检查 `/etc/sudoers.d/colima` 内容与当前版本模板一致；
3. 用 `sudo visudo -c` 验证语法；
4. 按当前系统策略使文件可被 Colima 检查，但不要放宽目录或二进制权限；
5. 重新 stop/start 后再检查 daemon 参数。

不要直接复制其他机器、其他 Colima 版本的 sudoers 文件。

## 5.4 配置文件中必须同时存在的三项

Colima profile 配置通常位于：

```bash
colima template --profile k3s --print 2>/dev/null || true
ls -l "$HOME/.colima/k3s/colima.yaml"
```

检查配置：

```bash
sed -n '/^network:/,/^[^[:space:]]/p' "$HOME/.colima/k3s/colima.yaml"
```

必须确认以下三项都存在且含义正确：

```yaml
network:
  address: true
  mode: bridged
  interface: en1
```

`address: true` 是关键开关；仅设置 `mode: bridged` 可能仍然启动 shared NAT。启动命令中的 `--network-address`、`--network-mode bridged`、`--network-interface en1` 三者应保持一致。

## 5.5 启动并确认真实 LAN 地址

如果上一步已经启动 profile，先停止再用保存的配置启动；这样可以确认生成的配置确实被读取：

```bash
colima stop --profile k3s
colima start --profile k3s --network-address --very-verbose
colima status --profile k3s --extended
```

检查宿主机上的 vmnet daemon：

```bash
ps aux | grep '[s]ocket_vmnet'
```

预期参数中应出现类似：

```text
--vmnet-mode bridged --vmnet-interface en1
```

检查 VM 内的网络：

```bash
colima ssh --profile k3s -- ip -4 -br addr show
colima ssh --profile k3s -- ip route
```

预期会看到一个 `col0` 或平台实际生成的 bridged 接口，并获得 LAN 网段地址，例如 `192.168.3.x/24`；同时可能还存在 Colima 内部的 `eth0`，不要把内部 NAT 地址误当成 LAN 地址。

如果 VM 没有 bridged 地址，按顺序执行：

```bash
colima stop --profile k3s
colima start --profile k3s --network-address --network-mode bridged --network-interface "$HOST_IF" --very-verbose
ps aux | grep '[s]ocket_vmnet'
colima ssh --profile k3s -- ip -4 -br addr show
```

然后检查日志中是否有 `error preparing sudoers directory`、`vmnet socket file not found` 或 daemon 启动失败。没有 `col0` 时不要继续安装 k3s/Cilium。

## 5.6 从 LAN 验证可达性

从 Mac 主机先获取 VM 的 LAN 地址，然后验证 PVE API 或其他 LAN 服务；不要把 PodIP/ClusterIP 当成宿主机可访问地址：

```bash
export COLIMA_LAN_IP=<从 colima ssh 输出获取的 LAN IP>
curl -sk -m 3 -o /dev/null -w '%{http_code}\n' "https://192.168.3.10:8006"
```

从 PVE 或另一台 LAN 主机验证：

```bash
ssh root@<PVE_HOST> "ping -c 3 ${COLIMA_LAN_IP} || true"
ssh root@<PVE_HOST> "curl -sk -m 3 -o /dev/null -w '%{http_code}\\n' https://${COLIMA_LAN_IP}:22 || true"
```

第二条对 SSH 端口通常返回非 HTTP 状态或连接错误，不适合作为应用层判断；更可靠的方式是在 Colima VM 内启动一个临时 HTTP 服务，从 PVE 对其端口进行 TCP/HTTP 连接测试。重点是确认 PVE 能路由到 VM 的 LAN IP。

## 5.7 在 Colima VM 内安装 k3s

不要使用 `colima start --kubernetes`，除非你明确要使用 Colima 自己管理的 k3s 且不接入本仓库。

先进入 VM，确认 hostname、LAN IP、DNS 和时间：

```bash
colima ssh --profile k3s
hostnamectl
ip -4 -br addr show
ip route
getent hosts github.com
```

在 VM 内按 [06 初始化 Linux](06-os-init.md) 完成系统初始化，再按角色阅读：

- 作为控制面：阅读 [07 k3s server](07-k3s-server.md)；
- 作为工作节点：阅读 [09 worker](09-worker.md)；
- CNI 首次安装：阅读 [08 Cilium](08-cilium.md)。

server 的 `--node-ip`、`--advertise-address` 和 `--tls-san` 应使用 Colima VM 的 LAN IP，而不是 `192.168.5.x` 内部 NAT 地址。

## 5.8 Colima 的常见坑

- 忘记 `--network-address`：profile 看似写了 bridged，实际仍是 shared NAT；
- `network.interface` 写成 `en0`，但 Mac 的真实出口接口是 `en1`；
- 只重启容器 runtime，没有 stop/start Colima，旧 lima network 配置仍被使用；
- sudoers 是旧版本内容或当前用户不可读，Colima 反复尝试安装后静默回退；
- 企业 Wi-Fi/VPN 禁止二层桥接，需要改用有线网卡、PVE VM 或 shared NAT + 端口转发；
- VM 能访问外网但 PVE 访问不到 VM，说明使用的是 shared NAT，不是本章目标；
- Mac host 能访问 Colima VM，但其他 LAN 设备不能访问，优先检查 socket_vmnet、Wi-Fi 隔离和交换机/路由器 DHCP 策略。

## 5.9 删除 profile

删除是破坏性操作，会删除 Colima VM 内的容器数据、磁盘和可能存在的 k3s 状态。确认没有需要保留的数据后，逐条执行并验证：

```bash
colima stop --profile k3s
colima delete --profile k3s
```

这不会删除 PVE VM，也不会删除本仓库的 Kubernetes 资源。
