#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib.sh"

mkdir -p "$ROOT_DIR/rendered"
export BASE_DOMAIN LB_POOL_START LB_POOL_STOP
python3 - "$ROOT_DIR" <<'PY'
from pathlib import Path
import os
root = Path(__import__('sys').argv[1])
for src, dst in [('config/cilium/lb.yaml.in', 'rendered/cilium.yaml'), ('config/gateway/gateway.yaml.in', 'rendered/gateway-api.yaml')]:
    text = (root / src).read_text()
    for key in ('BASE_DOMAIN', 'LB_POOL_START', 'LB_POOL_STOP'):
        text = text.replace('${' + key + '}', os.environ[key])
    (root / dst).write_text(text)
PY
cp "$ROOT_DIR/config/gateway/gateway-api-crds.txt" "$ROOT_DIR/rendered/gateway-api-crds.txt"
sed "s/\${BASE_DOMAIN}/$BASE_DOMAIN/g" "$ROOT_DIR/helm/gitea/values.yaml" > "$ROOT_DIR/rendered/gitea-values.yaml"
cp "$ROOT_DIR/helm/argocd/values.yaml" "$ROOT_DIR/rendered/argocd-values.yaml"
printf 'Rendered manifests and Helm values under rendered/\n'
