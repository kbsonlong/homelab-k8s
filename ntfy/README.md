# ntfy（自托管通知服务）

部署位置：**VM100（192.168.3.16）docker**，不进 k8s。
原因见 `docs/` 与 homelab skill：告警链路必须独立于集群存活，集群挂掉时恰是最需要通知的时刻。

## 端口与路径

- 宿主端口：`2586`（容器 80；5005/8000/1080/1087 等已被占用）
- 数据目录：`/data/ntfy/cache`（cache.db + attachments）、`/data/ntfy/etc`（server.yml）
- 仅 LAN / Tailscale 访问，**未** 接入 cf-tunnel，无公网暴露

## 部署

```bash
# VM100 上
mkdir -p /data/ntfy/cache /data/ntfy/etc
# 从本仓库同步 server.yml 到 /data/ntfy/etc/server.yml
cd /data/ntfy && docker compose up -d
```

## 验证

```bash
# 健康检查
curl -s http://192.168.3.16:2586/v1/health

# 发布/订阅回环
curl -d "test" http://192.168.3.16:2586/test-topic
curl -s http://192.168.3.16:2586/test-topic/json?poll=1
```

## 回滚

```bash
cd /data/ntfy && docker compose down
# 数据在 /data/ntfy，不会因容器删除丢失
```

## 认证（后续可选）

当前 LAN 内匿名可发布。如需收紧，在 `server.yml` 追加：

```yaml
auth-file: "/var/cache/ntfy/user.db"
auth-default-access: "deny-all"
```

然后容器内 `ntfy user add --role=admin <name>` 并重启。
