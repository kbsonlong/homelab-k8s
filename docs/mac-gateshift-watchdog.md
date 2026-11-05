# Mac mini 网关故障切换守护（gateshift-watchdog）

上线时间：2026-11-06。Mac mini (.18) 默认网关指向旁路由 VM100 (.16)，本守护在 VM100/v2ray 不可用时自动切回主路由 .1，恢复后自动切回 .16，形成闭环。

## 组件

| 文件 | 说明 |
|---|---|
| `/usr/local/bin/gateshift-watchdog.sh` | 守护脚本（root 运行，源码备份 `~/.hermes/scripts/gateshift-watch/`） |
| `/Library/LaunchDaemons/com.kbsonlong.gateshift-watchdog.plist` | root LaunchDaemon，RunAtLoad + KeepAlive |
| `/var/log/gateshift-watchdog.log` | 守护日志 |
| `~/.local/bin/gateshift` | GateShift v0.1.8（仅手动用；config: proxy=.16 / default=.1） |

## 逻辑

- 每 15s 探测 `192.168.3.16:1087`（TCP 连接 v2ray HTTP proxy 入口）。
  - 用 TCP 而非 ping：VM 活着但 v2ray 进程挂掉时同样触发切换。
  - 目标在同一 LAN 子网，探测包二层直达 .16，不经过任何网关，无代理环路。
- **FAILOVER**：连续 3 次失败且当前网关=.16 → `/sbin/route change default 192.168.3.1`。
- **RECOVER**：走 .1 期间连续 10 次（约 2.5min）探测成功 → `route change default 192.168.3.16`。
- 切换用 `route change` 而非 GateShift 的 `proxy/default` 命令：后者在 macOS 走 `networksetup -setmanual`，会把 Wi-Fi 从 DHCP 改成手动配置。

## 为什么任意设备只改网关即可分流（无需改 DNS）

- **任意 LAN 设备**把 IPv4 网关改为 `.16` 即获得同款分流；DNS 保持 DHCP 下发的路由器 `.1` 不用动（Mac Wi-Fi 无手动 DNS，解析器 = `.1`，DNS 查询走 LAN 直达路由器，不经过 VM100）。
- 分流不依赖客户端 DNS 结果：TCP 80/443 在 VM100 PREROUTING 被 REDIRECT 到 v2ray transparent inbound（dokodemo-door + followRedirect），v2ray 靠 **TLS SNI / HTTP Host 嗅探**还原域名，按 geosite 域名规则选 proxy/direct。
- 被墙域名即使被 DNS 污染返回假 IP：连接仍被透明拦截 → 嗅探出真域名 → 走 proxy 出口，由美国 VPS 远端解析真实 IP，污染结果被丢弃。
- 国内域名直连场景：域名无污染，DNS 正常，连接 RETURN 直出。
- 未命中任何路由规则的流量走默认 outbound（`proxy`，outbounds 首位）→ 海外未知站点自动走美国；geosite:cn 之外的国内新站会被误代理，需手动加直连规则。

## 两个前提/边界（"任意设备"的例外）

1. **IPv6 旁路**：透明链是纯 IPv4。设备若拿到路由器下发的 IPv6 RA 且站点支持 v6（Google 系全部支持），国外流量会绕过 v4 透明代理直连——Mac 已 `setv6off` 就是为堵这个。其他设备要么关 v6，要么在路由器侧停发 v6 RA。
2. **UDP/QUIC(443) 不在拦截范围**：应用 QUIC 失败后自动回退 TCP，无泄漏；非 80/443 端口的应用需显式代理（`.16:1087` / `.16:1080`）。

## 验证

```bash
pgrep -lf gateshift-watchdog.sh          # 守护进程
tail /var/log/gateshift-watchdog.log     # start 行 + 稳态无动作
/sbin/route -n get default | grep gateway   # 应为 192.168.3.16
```

模拟故障测试（会真实断网 45s）：`sudo pkill -STOP -f v2ray` 前先在 VM100 上操作，不建议随意演练；干跑方式见源码头部 `GATESHIFT_DRYRUN=1`。

## 回滚

```bash
sudo launchctl unload -w /Library/LaunchDaemons/com.kbsonlong.gateshift-watchdog.plist
sudo rm /Library/LaunchDaemons/com.kbsonlong.gateshift-watchdog.plist /usr/local/bin/gateshift-watchdog.sh
```

## 已知注意事项

- `com.hermes.splitrouted.plist` 是 **EasyVPN 公司分流守护**（corp_dns/yy.com zones，apply 模式改路由），与本守护无关，勿混淆或同时排障。
- 2026-11-06 起 VM100 root SSH 密钥认证被拒（此前可用），登 VM100 需另行恢复密钥或走 PVE 控制台。
- VM100 侧配置细节见 `vm100-side-router-v2ray-iptables.md`。
