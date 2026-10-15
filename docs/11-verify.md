# 11 部署后检查与访问验证

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

### 8.1 集群与节点

```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl get storageclass
```

所有预期节点应为 `Ready`。`local-path` 应存在，但要记住它是节点本地存储。

### 8.2 Cilium、VIP 和 Gateway

```bash
kubectl get gatewayclass cilium
kubectl get gateway -A
kubectl get httproute -A
kubectl get svc -A | grep LoadBalancer
kubectl get ciliuml2announcementpolicy,ciliumloadbalancerippool
kubectl -n kube-system exec ds/cilium -- cilium-dbg status
```

预期：

- `GatewayClass/cilium` 为 `Accepted=True`；
- `lab-gateway` 为 `Programmed=True` 并获得 VIP；
- Argo CD 和 Gitea HTTP 服务是 `ClusterIP`；
- 只有 Gitea SSH 作为业务 `LoadBalancer`；
- HTTPRoute 的 `Accepted=True`、`ResolvedRefs=True`。

### 8.3 应用与访问

```bash
make status
make verify
```

没有 DNS 时，先从 `make verify` 的 VIP 和域名规划确认服务，再手工测试：

```bash
curl --noproxy '*' -i -H 'Host: gitea.example.com' http://<GATEWAY_VIP>/
curl --noproxy '*' -i -H 'Host: argocd.example.com' http://<GATEWAY_VIP>/
```

如果使用真实域名，在 DNS 中把 `gitea.<BASE_DOMAIN>` 和 `argocd.<BASE_DOMAIN>` 指向 Gateway VIP；公网访问则把公网入口指向 Cloudflare Tunnel 或其他明确的边界代理，不要直接暴露 Kubernetes API。

### 8.4 获取 Argo CD 初始密码

Argo CD 的初始管理员密码只在受控终端显示：

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 --decode; printf '\n'
```

首次登录后立即修改密码，并通过密码管理器保存。不要把输出写入 shell 历史、截图或 Git。
