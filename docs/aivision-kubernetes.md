# AI Vision on homelab Kubernetes

This directory runs the already-built AI Vision images as native Kubernetes
workloads. It does **not** convert a running Docker container into a Pod:
images are reused, while Kubernetes owns the workload, Service, storage and
Gateway routing.

## Current target

- Cluster context: `lab-default`
- Node: `colima` (`arm64`, `192.168.3.83`)
- Shared HTTP Gateway: `cilium-gateway/lab-gateway`
- Gateway VIP: `192.168.3.243`
- Image registry: `192.168.3.83:5050`
- Database: seekdb arm64 digest `sha256:7493553b169c0fb8c5eb63db42be082f8c6dc02fe78a26eec62a6c6ab213c59d`

The manifests intentionally pin workloads to `colima`: the seekdb data PV and
local metastore are local paths in the Colima VM and are not portable to
`lab-node`.

## Important migration boundary

The current Docker stack remains the rollback source until K8s verification
passes. Do not run `aiv destroy --purge` or remove
`aivforge-seekdb-data-aivision`.

The seekdb PV points at the Docker volume data directory:

```text
/var/lib/docker/volumes/aivforge-seekdb-data-aivision/_data
```

Kubernetes must only mount it after the Docker `oceanbase` container is stopped.
The SigNoz metastore PV points at:

```text
/Users/kbsonlong/.hermes/aivision-forge/aivision/state/signoz-metastore
```

## Preflight and image pull

The local registry is reachable from the Colima node at
`http://192.168.3.83:5050`. K3s must permit the plain-HTTP registry. Configure
`/etc/rancher/k3s/registries.yaml` on the Colima node with:

```yaml
mirrors:
  "192.168.3.83:5050":
    endpoint:
      - "http://192.168.3.83:5050"
```

Restart the K3s server after changing it. If the runtime already has this
configuration, only verify it; do not overwrite unrelated registry settings.

## Migration procedure

### 1. Render and inspect

```bash
kubectl config use-context lab-default
kubectl kustomize apps/aivision > /tmp/aivision-rendered.yaml
kubectl apply --dry-run=client -k apps/aivision
```

Create the Secret from the currently running Docker stack. The script reads
secrets locally and sends them directly to the Kubernetes API; its output is
not committed:

```bash
chmod 700 apps/aivision/scripts/create-secret-from-docker.sh
kubectl apply -f apps/aivision/namespace.yaml
apps/aivision/scripts/create-secret-from-docker.sh
```

### 2. Create storage and verify the data paths

```bash
kubectl apply -f apps/aivision/storage.yaml
kubectl -n aivision get pv,pvc
```

Before any Docker stop, verify the Docker database volume and the K8s PV point
to the intended paths. The seekdb database volume is about 2.2 GiB in the
current deployment.

### 3. Pull-test all application images

```bash
kubectl apply -k apps/aivision
kubectl -n aivision get pods -o wide
kubectl -n aivision describe pod -l app.kubernetes.io/part-of=ai-vision
```

Do not proceed if any image reports `ErrImagePull` or `ImagePullBackOff`.

### 4. Cut over database ownership

This is the first state-changing migration step. Keep the Docker volumes.
Stop only the Docker application stack, then let the StatefulSet own the seekdb
volume:

```bash
docker stop aivision-fe agentlens aivision-be antcollector-gw signoz-ob oceanbase
kubectl -n aivision rollout status statefulset/seekdb --timeout=10m
kubectl -n aivision rollout status deployment/signoz-ob --timeout=10m
```

If seekdb does not become Ready, immediately stop the K8s seekdb Pod and restart
the original Docker stack; do not delete the PV or volume.

### 5. Start and verify the application workloads

```bash
kubectl -n aivision rollout status deployment/antcollector-gw --timeout=5m
kubectl -n aivision rollout status deployment/aivision-be --timeout=10m
kubectl -n aivision rollout status deployment/agentlens --timeout=10m
kubectl -n aivision rollout status deployment/aivision-fe --timeout=5m
kubectl -n aivision get pods,svc,pv,pvc -o wide
```

Backend bootstrap is idempotent, but the first run may take several minutes.
Inspect logs if readiness does not converge:

```bash
kubectl -n aivision logs deploy/aivision-be --tail=200
kubectl -n aivision logs deploy/agentlens --tail=200
```

### 6. Gateway verification

The intended HTTP endpoints are:

```text
http://aivision.kbsonlong.com
http://otel-aivision.kbsonlong.com/v1/traces
```

The routes use the existing shared Gateway and do not create a new LoadBalancer:

```bash
kubectl apply -f apps/aivision/gateway-routes.yaml
kubectl -n aivision get httproute grpcroute -o wide
kubectl -n cilium-gateway get gateway lab-gateway -o wide
```

For LAN-only validation, add these host mappings on the client:

```text
192.168.3.243 aivision.kbsonlong.com otel-aivision.kbsonlong.com
```

Then verify:

```bash
curl -i --noproxy '*' -H 'Host: aivision.kbsonlong.com' http://192.168.3.243/
curl -i --noproxy '*' -H 'Host: otel-aivision.kbsonlong.com' http://192.168.3.243/v1/traces
```

A trace endpoint request without an ingest token should return `401`; that
proves the route reaches the gateway, not that a trace was ingested.

### 7. Rollback

If K8s fails before data validation:

```bash
kubectl -n aivision scale statefulset/seekdb --replicas=0
kubectl -n aivision scale deployment --all --replicas=0
docker start oceanbase signoz-ob antcollector-gw aivision-be agentlens aivision-fe
```

Validate the Docker stack with the existing `aiv status` command. Do not remove
K8s PVC/PV or Docker volumes during rollback.

## Resource notes

This is a single-node local-path deployment. seekdb has a 2 GiB runtime memory
parameter and a 3 GiB Kubernetes memory limit; other workloads have bounded
requests/limits. Do not schedule this stack to `lab-node` unless storage is
migrated to a real shared/remote volume and the arm64-only seekdb constraint is
resolved.

## Known limitations

- Native Langfuse v1 history start is carried over, but this hand-written K8s
  deployment is not covered by the official Kubernetes `aiv` target.
- OTLP/gRPC is exposed as a `GRPCRoute` on the shared HTTP Gateway and must be
  validated with the actual SDK; keep the Docker port closed during normal K8s
  operation unless that validation succeeds.
- Registry `5050` is a local image-distribution endpoint, not a public app route.
- Secrets and generated kubeconfig material must never be committed.
