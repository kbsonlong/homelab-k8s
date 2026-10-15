# 13 回滚、排障与销毁

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

### 10.1 常用排查顺序

```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl describe pod <pod> -n <namespace>
kubectl get events -A --sort-by=.lastTimestamp
kubectl get gateway,httproute -A
kubectl get svc -A
kubectl -n kube-system logs ds/cilium --all-containers --tail=100
```

VIP 为 `<pending>` 时，依次检查：

1. Cilium operator 是否健康；
2. IP pool 是否与 DHCP 或其他主机冲突；
3. L2 policy 的网卡是否是节点实际使用的接口；
4. 节点是否处于同一可达二层网络；
5. Cilium agent/operator 是否在配置改变后重新启动。

Gateway 为 `Programmed=True` 但返回 404 时，检查：

1. 请求的 Host 是否和 HTTPRoute hostname 完全一致；
2. HTTPRoute 的 `Accepted` 和 `ResolvedRefs`；
3. backend Service 名称、端口和 EndpointSlice；
4. Envoy/Cilium 日志。

### 10.2 应用回滚

```bash
helm history argo-cd -n argocd
helm rollback argo-cd <REVISION> -n argocd
helm history gitea -n gitea
helm rollback gitea <REVISION> -n gitea
```

先保存 `kubectl get ... -o yaml` 和相关日志，再删除资源。不要用 `rm -rf /var/lib/rancher/k3s` 作为第一反应。

### 10.3 销毁本仓库部署的应用

这是破坏性操作，会删除本项目创建的 namespace 和资源。先确认没有需要保留的 Gitea/Argo CD 数据：

```bash
CONFIRM=destroy make destroy
```

该命令不负责删除 k3s、VM、PVE 主机或外部边界代理。删除 VM 前必须先确认备份、快照、DNS、Cloudflare Tunnel、固定 DHCP lease 和其他服务依赖；这一步不可逆，应逐台执行，不要批量复制破坏性命令。
