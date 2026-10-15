# 04 PVE：下载 raw 镜像、cloud-init 与 `qm` CLI

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

本章使用 PVE 命令行从 Ubuntu Cloud Image 创建可重复的 VM：下载并校验镜像、转换为 raw、导入磁盘、挂载 cloud-init、注入 SSH 公钥、配置 DHCP、启动检查，并创建可克隆模板。

> 命令分为“部署机”和“PVE 节点”两类。看到 `PVE 节点` 时，命令应在 PVE shell 执行；看到“部署机”时，命令应在自己的 Mac/Linux 终端执行。本文不会执行任何 VM 创建或删除操作。

## 4.1 前置检查

在部署机上准备 SSH 公钥，并确认能够登录 PVE：

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@<PVE_HOST> 'pveversion --verbose; pvesm status'
```

PVE 节点上确认存储支持的内容类型：

```bash
pvesm status
pvesm list local --content snippets 2>/dev/null || true
pvesm config local
```

你需要准备：

```text
PVE_HOST       = 192.168.3.10       # 示例，替换为 PVE 地址
PVE_NODE       = pve                # qm 命令所在节点名
VMID_TEMPLATE  = 9000               # 未占用的模板 ID
VMID_SERVER   = 201                 # 未占用的 server VM ID
STORAGE        = local-lvm          # 支持 images/rootdir 的存储
SNIPPET_STORE  = local              # 必须支持 snippets
BRIDGE         = vmbr0              # 接入目标 LAN 的 Linux bridge
```

如果 `SNIPPET_STORE` 不支持 `snippets`，不要直接修改生产存储配置。先确认该存储的用途、集群节点可见性和备份策略；`cicustom` 文件必须位于支持 snippets 的 PVE storage，迁移/HA 时还要确保目标节点可访问。

## 4.2 下载并校验 Ubuntu Cloud Image

下面示例使用 Ubuntu 24.04 LTS amd64 released image。不要使用 `daily` URL 作为长期模板；升级时显式更新版本并重新校验。

在 PVE 节点执行：

```bash
set -Eeuo pipefail

export IMAGE_DIR=/var/lib/vz/template/iso
export UBUNTU_RELEASE=24.04
export IMAGE_NAME=ubuntu-24.04-server-cloudimg-amd64.img

install -d -m 0755 "$IMAGE_DIR"
cd "$IMAGE_DIR"
curl -fLO "https://cloud-images.ubuntu.com/releases/${UBUNTU_RELEASE}/release/${IMAGE_NAME}"
curl -fLO "https://cloud-images.ubuntu.com/releases/${UBUNTU_RELEASE}/release/SHA256SUMS"
grep "[[:space:]]${IMAGE_NAME}$" SHA256SUMS | sha256sum -c -
qemu-img info "$IMAGE_NAME"
```

预期最后一行显示可识别的 qcow2/raw image 信息，校验输出为：

```text
ubuntu-24.04-server-cloudimg-amd64.img: OK
```

文件名虽然以 `.img` 结尾，实际格式以 `qemu-img info` 为准。不要仅根据扩展名判断格式，也不要跳过 SHA256 校验。

## 4.3 转换为 raw

为了让导入格式明确，并便于理解“下载镜像”和“PVE VM 磁盘”是两个不同对象，显式转换一份 raw 文件：

```bash
export RAW_IMAGE=/var/lib/vz/template/iso/ubuntu-24.04-server-cloudimg-amd64.raw
qemu-img convert -p -O raw \
  /var/lib/vz/template/iso/ubuntu-24.04-server-cloudimg-amd64.img \
  "$RAW_IMAGE"
qemu-img info "$RAW_IMAGE"
```

如果 `qemu-img info` 显示下载镜像本身已经是 raw，也可以直接复制或直接导入，但仍建议保留校验记录。raw 文件可能占用比稀疏 qcow2 更大的逻辑空间；确认 PVE 存储容量后再转换。

## 4.4 创建 VM 并导入系统盘

在 PVE 节点设置变量。以下资源适合 server/agent 入门，不是硬性规格：

```bash
set -Eeuo pipefail

export VMID=9000
export VM_NAME=k3s-ubuntu-template
export STORAGE=local-lvm
export SNIPPET_STORE=local
export BRIDGE=vmbr0
export RAW_IMAGE=/var/lib/vz/template/iso/ubuntu-24.04-server-cloudimg-amd64.raw

qm status "$VMID" >/dev/null 2>&1 && { printf 'VMID already exists: %s\n' "$VMID" >&2; exit 1; } || true

qm create "$VMID" \
  --name "$VM_NAME" \
  --ostype l26 \
  --machine q35 \
  --sockets 1 \
  --cores 4 \
  --memory 8192 \
  --agent enabled=1 \
  --onboot 1 \
  --scsihw virtio-scsi-single \
  --net0 "virtio,bridge=${BRIDGE},firewall=1"

qm disk import "$VMID" "$RAW_IMAGE" "$STORAGE" \
  --format raw \
  --target-disk scsi0

# qm disk import --target-disk scsi0 通常会直接挂载为 scsi0。
# 不要手写卷名；先从 qm config 确认导入结果。
qm config "$VMID"

qm set "$VMID" \
  --boot order=scsi0 \
  --serial0 socket \
  --vga serial0 \
  --ide2 "${STORAGE}:cloudinit" \
  --citype nocloud

qm config "$VMID"
```

`qm disk import --target-disk scsi0` 在当前 PVE 版本会把导入盘放到 `scsi0`。如果你的 PVE 版本仍显示 `unused0`，不要猜磁盘卷名；先查看：

```bash
qm config "$VMID"
```

再根据输出中的真实 `unused0` volume ID 执行一次明确的 attach，例如：

```bash
qm set "$VMID" --scsi0 '<从 qm config 读取的真实 unused0 volume ID>,discard=on,ssd=1'
```

不要把示例卷名直接复制到生产节点。

## 4.5 写入 cloud-init user-data

先把部署机的公钥复制到 PVE snippets 目录。公钥可以是 `id_ed25519.pub` 或其他 OpenSSH 公钥；不要复制私钥：

```bash
scp -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 \
  ~/.ssh/id_ed25519.pub \
  root@<PVE_HOST>:/var/lib/vz/snippets/k3s-authorized-key.pub
```

在 PVE 节点创建通用 user-data。模板中不写死 hostname 和 IP，让每个 clone 的 Proxmox metadata/network config 负责这些值：

```bash
set -Eeuo pipefail

export SNIPPET_STORE=local
export USER_DATA=/var/lib/vz/snippets/k3s-user-data.yaml
export SSH_PUBKEY_FILE=/var/lib/vz/snippets/k3s-authorized-key.pub

install -d -m 0755 /var/lib/vz/snippets

cat > "$USER_DATA" <<EOF
#cloud-config
users:
  - name: kubeadmin
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(cat "$SSH_PUBKEY_FILE")
ssh_pwauth: false
disable_root: true
package_update: true
packages:
  - ca-certificates
  - curl
  - jq
  - open-iscsi
  - nfs-common
  - qemu-guest-agent
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
EOF

chmod 0644 "$USER_DATA"
cat "$USER_DATA"
```

这是实验室初始化模板。生产环境应进一步收紧 `sudo` 权限、使用配置管理和短期凭据，不应长期保留 `NOPASSWD:ALL`。

把 custom user-data、SSH key、cloud-init drive 和 DHCP 设置挂到模板：

```bash
export VMID=9000
export SNIPPET_STORE=local
export USER_DATA_FILE=k3s-user-data.yaml

qm set "$VMID" \
  --cicustom "user=${SNIPPET_STORE}:snippets/${USER_DATA_FILE}" \
  --ipconfig0 ip=dhcp \
  --ciuser kubeadmin \
  --sshkeys /var/lib/vz/snippets/k3s-authorized-key.pub \
  --citype nocloud

qm cloudinit update "$VMID"
qm cloudinit dump "$VMID" user
qm cloudinit dump "$VMID" network
qm cloudinit dump "$VMID" meta
```

注意：`cicustom user=` 会替换 Proxmox 自动生成的完整 user-data。需要的用户、SSH key、包和初始化动作必须写进 custom 文件；不要以为只写一段 `runcmd` 就会自动保留所有 GUI 设置。

## 4.6 首次启动并验证 cloud-init

先启动模板 VM 做一次验证，不要在未验证时直接标记为 template：

```bash
qm start 9000
qm status 9000
qm terminal 9000
```

如果使用 SSH 而不是 serial console，先从路由器 DHCP lease、PVE bridge 邻居表或 guest agent 获取地址：

```bash
qm guest cmd 9000 network-get-interfaces
ip neigh show dev vmbr0
```

guest agent 命令要求镜像内的 `qemu-guest-agent` 已启动；cloud-init 尚未完成时应以 DHCP lease 和邻居表为准。

拿到地址后，在部署机验证：

```bash
ssh -o IdentitiesOnly=yes kubeadmin@<VM_IP> 'hostname; cloud-init status --wait; systemctl is-active qemu-guest-agent; ip -br address'
```

在 VM 内确认：

```bash
cloud-init status --long
cloud-init analyze show
systemctl is-active qemu-guest-agent
ip -br address
ip route
getent hosts github.com
```

预期：`cloud-init status --long` 为 `status: done`，用户可通过 SSH key 登录，网络使用预期 LAN 地址。

## 4.7 清理验证 VM 并制作模板

只有确认 cloud-init 每次启动都能正确生成用户、SSH 和网络后，才把 VM 转为模板。先在 guest 内清理 cloud-init 状态：

```bash
sudo cloud-init clean --logs --seed
sudo shutdown -h now
```

在 PVE 节点确认 VM 已停止，然后转换为模板：

```bash
qm status 9000
qm template 9000
qm config 9000
```

`qm template` 是状态变化操作，会使该 VM 不能按普通 VM 直接启动；后续应通过 clone 使用。模板本身不要承载业务数据。

## 4.8 从模板创建 k3s 节点

在 PVE 节点执行一次 clone：

```bash
qm clone 9000 201 \
  --name k3s-server-01 \
  --full 1 \
  --storage local-lvm

qm set 201 \
  --cores 4 \
  --memory 8192 \
  --onboot 1 \
  --ipconfig0 ip=dhcp

qm start 201
qm status 201
```

如果使用静态地址而非 DHCP lease，使用 CIDR 和同网段网关：

```bash
qm set 201 --ipconfig0 ip=192.168.3.83/24,gw=192.168.3.1
```

不要同时在路由器 DHCP、cloud-init network-data 和 VM 内 Netplan 中重复定义不同地址。推荐入门环境使用 DHCP reservation：地址由路由器固定，VM 仍使用 `ip=dhcp`。

创建 agent 时使用不同 VM ID、hostname 和 DHCP reservation：

```bash
qm clone 9000 202 --name k3s-agent-01 --full 1 --storage local-lvm
qm set 202 --cores 4 --memory 8192 --onboot 1 --ipconfig0 ip=dhcp
qm start 202
```

从这里继续阅读：

- 初始化每个 guest：[`06-os-init.md`](06-os-init.md)；
- server 安装：[`07-k3s-server.md`](07-k3s-server.md)；
- agent 加入：[`09-worker.md`](09-worker.md)。

## 4.9 回滚和危险操作

- `qm stop`、磁盘 detach 和模板转换前先确认 VM ID；
- `qm destroy` 会删除配置和磁盘，必须单独确认、单独执行；
- 删除前先检查 `qm config <VMID>`、备份、快照、DHCP lease、DNS 和其他服务依赖；
- 不要把 `qm destroy` 放进批量脚本或和清理临时文件的命令打包执行。
