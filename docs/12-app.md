# 12 添加自己的应用

> 返回 [01 教程索引](01-from-vm-to-k3s.md)。

普通 HTTP 应用遵循以下边界：

1. 应用 Service 使用 `ClusterIP`；
2. 应用放在独立 namespace；
3. 通过一个 `HTTPRoute` 接入共享 Gateway；
4. 只有 SSH、数据库、MQTT 或自定义 TCP/UDP 等无法由 HTTPRoute 表达的协议，才使用独立 `LoadBalancer`；
5. 生产环境补上 NetworkPolicy、资源 requests/limits、备份和健康探针。

最小 HTTPRoute 示例：

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
  hostnames:
    - app.example.com
  rules:
    - backendRefs:
        - name: app
          port: 8080
```

提交应用前先渲染和检查：

```bash
kubectl apply --dry-run=server -f app.yaml
kubectl apply -f app.yaml
kubectl -n app rollout status deployment/app --timeout=300s
kubectl -n app get svc,endpointslice
```

长期运行的服务建议交给 Argo CD 管理，而不是手工在生产集群中反复执行 `kubectl apply`。
