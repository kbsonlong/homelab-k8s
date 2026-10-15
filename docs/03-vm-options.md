# 03 创建 Linux VM：通用要求与 PVE 图形界面

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

### 2.1 通用 VM 参数

无论使用哪种虚拟化平台，都创建一台支持以下条件的 VM：

- Ubuntu Server 24.04 LTS 或同等版本的 Debian/Ubuntu；
- UEFI 或 BIOS 均可，但后续节点保持一致；
- 网卡使用桥接模式，不能只使用 NAT；
- 磁盘使用 SSD、VirtIO/SCSI 或平台推荐的高性能总线；
- 使用 OpenSSH Server；
- 时钟通过 NTP 同步；
- VM 能解析域名并访问外网软件仓库；
- 生产/长期实验环境使用快照或备份，但不要把快照当成唯一备份。

不要把 k3s 节点放在互相隔离的 NAT 网络中。NAT 适合临时单节点练习，不适合 Cilium L2 广播和多个节点通信。

### 2.2 PVE 图形界面步骤

以下是 PVE 的通用流程，PVE 版本或存储名称不同也不影响原则：

1. 上传 Ubuntu Server ISO 到 PVE 存储；
2. 创建 VM，记录 VM ID 和名称，例如 `k3s-server-01`；
3. CPU 设置为至少 `2 cores`，类型优先选择 `host`；
4. 内存设置为至少 `4096 MiB`，启用 ballooning 前先确认不会导致节点频繁回收内存；
5. 磁盘至少 `40 GiB`，生产实验建议 `60 GiB` 或更大；
6. 网卡选择 VirtIO，Bridge 选择接入实验室 LAN 的桥，例如 `vmbr0`；
7. 安装 Ubuntu，创建普通管理员用户并启用 OpenSSH；
8. 安装完成后把网卡 MAC 与 VM 名称记录到节点清单；
9. 在 DHCP 中为 MAC 创建固定 lease，启动 VM 并确认获得预期 IP；
10. 如需 worker，按同样方式创建 `k3s-agent-01`，不要复制一个已经安装过 k3s 的磁盘直接使用。

VMware、VirtualBox、UTM、Lima、Colima 或云主机的对应选择是：桥接网络、固定地址、足够磁盘、可用 SSH。平台不同不应改变后续 k3s 拓扑。

如果希望自动下载 cloud image、注入 cloud-init 并批量创建节点，继续阅读 [`04-pve-qm-cloud-init.md`](04-pve-qm-cloud-init.md)。如果在 macOS 上需要 VM 直接获得 LAN 地址，阅读 [`05-colima-bridge.md`](05-colima-bridge.md)。

### 2.3 VM 首次检查

在每台 VM 上执行，先不要安装 k3s：

```bash
hostnamectl
ip -br address
ip route
resolvectl status 2>/dev/null || cat /etc/resolv.conf
timedatectl status
free -h
df -h /
ss -lntup
```

从 server VM 测试网络：

```bash
ping -c 3 <LAN_GATEWAY>
getent hosts github.com
curl -fsS https://get.k3s.io >/dev/null
```

从 agent VM 测试 server：

```bash
ping -c 3 <SERVER_IP>
curl -kfsS https://<SERVER_IP>:6443/version
```

如果最后一个命令失败，先修复网络、防火墙、地址或 k3s API 访问路径，不要继续安装 agent。
