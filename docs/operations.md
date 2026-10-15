# 日常运维与扩展

## 添加一个 HTTP 服务

推荐：只创建 `ClusterIP` Service 和一个 HTTPRoute，不创建新的 LoadBalancer。

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: app
  namespace: app
spec:
  parentRefs:
    - name: lab-gateway
      namespace: cilium-gateway
      sectionName: http
  hostnames: [app.example.com]
  rules:
    - backendRefs:
        - name: app
          port: 8080
```

`Gateway` 的 `allowedRoutes` 当前为 `namespaces.from: All`，适合实验室；多租户环境应改为 `Same` 或使用 `ReferenceGrant`/命名空间白名单。

## 添加四层服务

只有不能被 HTTPRoute 表达的服务才申请独立 LB，例如：

- Gitea SSH `22/TCP`
- PostgreSQL `5432/TCP`
- MQTT `1883/TCP`
- 自定义 TCP/UDP 协议

创建 `type: LoadBalancer` Service 前，先确认 IP pool 有空闲地址，并记录用途。不要让普通 Web 服务复制 Gateway 的 LB。

## 常用诊断

```bash
kubectl get gatewayclass cilium
kubectl get gateway -A
kubectl get httproute -A
kubectl get svc -A | grep LoadBalancer
kubectl get ciliuml2announcementpolicy,ciliumloadbalancerippool
kubectl -n kube-system exec ds/cilium -- cilium-dbg status
```

如果 VIP `<pending>`：

1. 检查 Cilium operator 是否健康；
2. 检查 pool 是否 `DISABLED=False, CONFLICTING=False`；
3. 检查 Service selector/端口；
4. 检查 L2 policy 的接口名；
5. 如 Helm 改过 Cilium ConfigMap，重启 agent/operator，Cilium 配置不是所有项都热加载。

如果 Gateway `Programmed=True` 但 HTTP 404：

1. 用正确的 `Host` 或 SNI 测试；
2. 查看 HTTPRoute `Accepted` 和 `ResolvedRefs`；
3. 查看 Cilium agent 的 Envoy xDS 日志；
4. 确认 backend Service 是 ClusterIP 且 EndpointSlice 有 Ready endpoint。

## 备份建议

local-path PVC 与节点绑定，不提供 HA。至少备份：

- Gitea `/data`；
- PostgreSQL 数据；
- Argo CD repo/secret 配置；
- GitHub 仓库本身。

不要把备份密钥提交到本仓库。