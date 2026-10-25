# VM100 旁路由、v2ray 与 iptables 配置解析

摸排对象：PVE VM100（Ubuntu 22.04.5，192.168.3.16），观察时间 2026-10-25。本文描述当时实际运行态和持久配置；若配置后来变更，以 VM100 实机回读为准。文档不保存代理 UUID、密码、Cloudflare 凭据或其他秘密值。

## 拓扑与职责

- VM100 网卡 `ens18`，LAN `192.168.3.16/24`，默认网关 `192.168.3.1`；IPv4 forwarding=1，IPv6 forwarding=0。宿主有公网 IPv6 RA 默认路由，但没有 IPv6 转发旁路链路。
- Mac mini `.18` 的显式 HTTP/SOCKS 代理目标分别是 `.16:1087` 与 `.16:1080`；Mac IPv4 默认网关仍是路由器 `.1`。VM100 是代理和按客户端配置的旁路由节点，不是全家 DHCP 默认网关。
- 主要组件：v2ray systemd、v2ray 透明 NAT oneshot、dnsmasq TCP DNS forwarder、Docker 应用、Tailscale。VM100 默认路由仍经 `.1`，SNAT/MASQUERADE 使用真实默认出口 `ens18`。

## v2ray

### 进程与文件

- Version：v2ray 5.47.0，二进制 `/usr/local/bin/v2ray`。
- 主配置：`/usr/local/etc/v2ray/config.json`；另有带时间后缀的旧配置备份，运行时以无后缀文件为准。
- systemd `v2ray.service` 以 root 运行，ExecStart `/usr/local/bin/v2ray run -config /usr/local/etc/v2ray/config.json`，具备 `CAP_NET_ADMIN`、`CAP_NET_BIND_SERVICE`。
- geo data：`/usr/local/bin/geoip.dat`、`/usr/local/bin/geosite.dat`。缺失时 `geoip:private` 等路由规则无法加载。
- 日志路径配置为 `/var/log/v2ray/error.log` 与 `access.log`。

### Inbound / outbound

- `dns-in`：UDP `*:53`，dokodemo-door，DNS 目标由配置指定。
- `transparent`：TCP/UDP `*:12345`，dokodemo-door + followRedirect，接收 iptables REDIRECT 的透明代理流量。
- SOCKS：`0.0.0.0:1080`，允许 UDP。
- HTTP proxy：`0.0.0.0:1087`。
- 出站包含 vmess `proxy`、`direct`、`block`、`dns-out`。proxy 的服务器地址、UUID、WebSocket path/host 均属敏感配置，本文不记录值。
- DNS 分流：默认上游 `223.5.5.5`；Google/Apple/GitHub/Telegram 域名走 `8.8.8.8`。路由规则先将 dns-in 交给 dns-out，再让 private IP 直连；Apple/Google/GitHub/Telegram 域名走 proxy；中国域名与中国 IP 直连。

### 代理策略

普通客户端可显式设置 HTTP `http://192.168.3.16:1087` 或 SOCKS `socks5h://192.168.3.16:1080`。显式代理请求成功只证明该代理入口，不代表透明代理、NAT 或客户端默认路由旁路已通过验收。

## iptables 与 ipset

### 持久配置来源

- 规则生成脚本：`/usr/local/bin/setup_v2ray_nat.sh`。
- systemd oneshot：`v2ray-iptables.service`，在 `network-online.target`、Docker 与 v2ray 之后执行该脚本，`RemainAfterExit=yes`。
- 脚本以 `ip route show default` 动态探测出口接口；当前为 `ens18`。它设置 IPv4 forwarding=1，并关闭 all/default/出口接口的 send_redirects。
- 持久规则还见 `/etc/iptables/rules.v4`、`rules.v6`、`iptables_rules.ipv4`、`ipset_rules.ipv4`。须分别检查持久化来源与运行态，不能只看 `iptables-save`。

### NAT 链逻辑

自定义链 `V2RAY_TRANSPARENT`：

1. 目标为本机地址时 RETURN。
2. 127/8、10/8、172.16/12、192.168/16 私网目标 RETURN。
3. 命中 `bypass_list` ipset 的公网 IP RETURN。
4. 其余 TCP/80 和 TCP/443 分别 REDIRECT 到 12345。

自定义链 `V2RAY_OUTPUT`：UID 65534 与 packet mark 255 RETURN，其余进入 `V2RAY_TRANSPARENT`。OUTPUT 入口还有 `owner ! --uid-owner 0` 限制，因此仅非 root 的本机 TCP 流量进入；root 发起的本机流量一般不经 OUTPUT 透明捕获。容器转发流量和本机 OUTPUT 是不同路径，需查看规则计数器验证。

PREROUTING 当前观察到：

- Mac `.18` 的 TCP/53 REDIRECT 到 5353；运行态有重复的同类规则，需要审查脚本的去重逻辑。
- 所有客户端 UDP/53 REDIRECT 到本机 53（v2ray DNS inbound）。
- 所有 PREROUTING TCP 进入 `V2RAY_TRANSPARENT`，链内仅 80/443 REDIRECT，其它端口 RETURN。
- Docker 也在 PREROUTING 建立 `DOCKER` 链，实际顺序需按运行态回读。

POSTROUTING 有 Docker bridges 的 MASQUERADE、Tailscale `ts-postrouting` 和 `-o ens18 -j MASQUERADE`。后者为经 VM 转发/出站的流量提供 SNAT；VM 默认网关仍须为路由器 `.1`。

FORWARD 默认 ACCEPT，经过 `ts-forward`、`DOCKER-USER`、`DOCKER-FORWARD`；Docker 与 Tailscale 链由各自服务管理。

### bypass_list

- `bypass_list` 类型 `hash:net`，脚本创建上限 65536。
- 集合为空时，脚本装入私网、DNS 和保底网段；国内 IP 列表约 4300 条，更新来源可能涉及 `/usr/local/bin/update-chnroute.sh`，需另外核对更新任务。
- 本次观察到运行态仅 14 条。不可把历史约 4300 条规模当作当前事实；以 `ipset list bypass_list` 和规则计数器为准判断分流覆盖。

## DNS：UDP 与 TCP 分开

- v2ray 占 `*:53/UDP`，UDP/53 由 PREROUTING redirect 到该 inbound。
- `dnsmasq-side-router.service` 使用 `/etc/dnsmasq.d/side-router.conf`，绑定 `192.168.3.16:5353`，转发到 `223.5.5.5`，提供 TCP/UDP 监听。目标客户端 `.18` 的 TCP/53 redirect 到 5353。
- 实测 VM100 UDP `.16:53` 查询成功；TCP `.16:5353` 查询成功。直接 `.16:53/TCP` 并非已验证路径。
- Docker `nestingdns` 包含 AdGuardHome、mosdns、smartdns 状态与规则；容器 healthy 不足以证明客户端 DNS 正在使用它。重建前须核实容器端口映射、内部监听及客户端 DNS 路径，避免与 v2ray 53 冲突。

## 旁路由流量路径

Mac `.18` 当前可使用显式代理：应用 → `.16:1087` 或 `.16:1080` → v2ray 按域名规则选 proxy/direct。若把客户端 IPv4 默认路由改为 `.16`，目标流量会进入 VM；私网直连，公网 HTTP/HTTPS 按 bypass_list 与端口重定向，其余协议按 FORWARD/NAT 路由。Mac 当前默认网关仍为 `.1`，故显式代理测试不能被描述为默认路由旁路验收。

IPv6 forwarding=0；客户端若仍有 IPv6 默认路由，IPv6 流量不会经过该 IPv4 旁路链。需单独建立完整 IPv6 路由、防火墙和回程后才可宣称 IPv6 旁路。

## 验收与故障定位

VM 侧基线：

```bash
ip -br addr
ip -4 route
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
systemctl status v2ray v2ray-iptables dnsmasq-side-router
ss -lntup
ipset list bypass_list | sed -n '1,8p'
iptables -t nat -vnL PREROUTING
iptables -t nat -vnL V2RAY_TRANSPARENT
iptables -t nat -vnL POSTROUTING
iptables -vnL FORWARD
```

单项探针：

```bash
dig @192.168.3.16 example.com A
dig +tcp @192.168.3.16 -p 5353 example.com A
curl -x http://192.168.3.16:1087 -I https://example.com
curl --socks5-hostname 192.168.3.16:1080 -I https://example.com
```

端到端旁路由验收还需让目标客户端实际使用 `.16` 作为 IPv4 默认网关，并检查 LAN、直连、代理分流、DNS UDP/TCP 和规则计数器。没有这一步时，只能报告 VM 服务或显式代理可用。

## 安全与维护

- 不要将代理 UUID、服务端地址、Cloudflare 凭据、Tailscale state 或 WebPush 私钥提交到 Git。
- 不要整体覆盖 `/etc/iptables` 或不加区分地恢复全部 `iptables-save`；Docker/Tailscale 动态链由各自服务维护。
- 512 MiB VM 曾因加载完整 v2ray geodata 和大规则集触发 OOM。即使精简规则下 HTTP proxy 成功，也不代表完整分流、透明 NAT 或旁路由验收通过。
- 修改生产 v2ray、DNS、iptables 或重启 VM100 前，按家庭实验室高危操作规程检查备份、依赖、影响与回滚。
