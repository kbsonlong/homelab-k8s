Use when deploying Xiaozhi server on the Colima k3s node. Build a native arm64 image on Colima, deploy only the server with API based ASR, and document local secrets and recovery.

# Xiaozhi server on Colima

## Layout and decisions
- Node: `colima` (`linux/arm64`), pinned by `kubernetes.io/hostname=colima`.
- Use the upstream multi-arch `ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest` image (manifest includes arm64); do not build locally unless this tag loses arm64 support.
- Store app data and model files on the external volume `/Volumes/nfs`, which Colima exposes at the same path via virtiofs. Since Kubernetes `hostPath` can reference only paths inside the node, use a static local PV pinned to Colima at `/Volumes/nfs/xiaozhi-server`.
- Deploy only the core server. Do not deploy manager web/API, MySQL, Redis, or the local FunASR model.
- 使用 API ASR。Doubao 凭据通过本地 Secret 注入，ConfigMap 的空值会由部署操作生成 runtime config；不得提交实际 API keys。
- Deployment 当前选择 Sherpa Paraformer ASR。该模型需要单独下载到挂载的持久卷；模型未就绪前，Pod 只作为 WS/OTA 服务验证，不宣称语音识别可用。
- Persistent PVC is node-local (`local-path`), 2 GiB. Back it up before rebuilding the Colima VM.
- HTTP management/OTA is reached through shared Gateway at `xiaozhi.kbsonlong.com`; WebSocket traffic is a separate LoadBalancer service with a reserved TCP VIP. Do not route WebSocket over HTTPRoute unless the firmware client explicitly uses WebSocket-over-HTTP on port 8003.

## Build and deploy
1. Check Colima status, Docker context, BuildKit builder, node architecture, free disk, and image visibility from k3s.
2. From the `xiaozhi-esp32-server` source root run:
   ```sh
   docker buildx build --builder colima --platform linux/arm64 --load -f Dockerfile-server -t local/xiaozhi-server:arm64 .
   ```
3. Verify the image exists in Colima Docker and is visible to the Colima Kubernetes runtime. If not visible, import it into the Colima k3s containerd image store and verify the exact tag before applying manifests.
4. Create namespace, PVC, ConfigMap, Deployment, Service and HTTPRoute from `apps/xiaozhi-server/namespace.yaml`. Store credentials only in a Secret created outside Git; patch the runtime config without exposing secret values in shell history or logs.
5. Apply Gateway TCP listener configuration only if it is required for firmware WebSocket clients, after verifying the selected VIP is available and L2 announcements are active on Colima. In this cluster, use the approved unused VIP from the pool; verify with `kubectl get svc -A -o wide` before assignment.
6. Verify PVC Bound, pod scheduled on Colima and Ready, app logs show WebSocket/OTA listeners, Service endpoints exist, HTTPRoute Accepted/ResolvedRefs, Gateway HTTP GET reaches the OTA endpoint, WebSocket handshake works from a real client, and TCP VIP is reachable from LAN.

## Rollback
Delete only the Xiaozhi Deployment, Service, Route, ConfigMap and namespace resources. Preserve the PVC until data has been backed up and the user explicitly requests removal. Do not modify the shared Gateway or Cilium LB pool for this rollback.

## Current constraints
- No API provider credentials have been supplied; ASR will not function until the Secret is populated.
- Do not enable a public Cloudflare route as part of this deployment; only LAN access through the existing Gateway is in scope.
- No local FunASR model is mounted or downloaded. Local ASR needs separate resource and model-storage planning.
