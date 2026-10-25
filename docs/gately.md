# Gately

Gately is deployed as a restricted single replica in the `gately` namespace.
The admin UI is exposed by the shared Cilium Gateway at
`gately.kbsonlong.com`; the HTTPRoute is ready for the existing Cloudflare
Tunnel but the tunnel hostname is not activated until VM100 configuration is
inspected and updated separately.

## Security and network boundary

- The image runs as non-root UID/GID 1000; the Pod uses a RuntimeDefault seccomp
  profile and adds no capabilities. The Deployment has no host networking,
  privileged mode, host ports, Docker socket, or host filesystem mounts.
- Gately's host-level port forwarding, STUN listeners, nftables integration,
  and Docker management are therefore not enabled in this Kubernetes profile.
- The UI can administer Gately itself, so use a unique bootstrap password,
  change it after first login, enable TOTP, and restrict public access with a
  Cloudflare Access policy before adding the Tunnel hostname.
- The bootstrap Secret is created from `secrets/gately-initial-password`; that
  directory is ignored by git. Never commit the password or generated Secret.
- Configuration and audit history persist in the `gately-config` PVC (2 GiB,
  local-path on the `colima` node). Back it up before upgrades or removal.

## Install

From the repository root, create a strong one-time password, then create the
Secret and application:

```bash
install -d -m 700 secrets
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/gately-initial-password
kubectl -n gately create secret generic gately-bootstrap \
  --from-file=initial-password=secrets/gately-initial-password \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f apps/gately.yaml
```

Read the generated password locally for first login, change it in Gately, and
enable TOTP.

## Verify

```bash
kubectl -n gately rollout status deployment/gately
kubectl -n gately get pods,pvc,service
kubectl -n gately get httproute gately-ui -o yaml
curl --resolve gately.kbsonlong.com:80:192.168.3.243 \
  -I http://gately.kbsonlong.com/
```

The route must report `Accepted=True` and `ResolvedRefs=True`; the local curl
must receive an HTTP response. The browser can use the same hostname after
adding `192.168.3.243 gately.kbsonlong.com` to its hosts file (the public
Cloudflare wildcard DNS otherwise points browsers away from the LAN gateway).

## Optional public entry

After the UI is healthy and Cloudflare Access is configured, add
`gately.kbsonlong.com` to the existing named tunnel on VM100 with service
`http://192.168.3.243:80`. This hostname is deliberately absent from tunnel
configuration until the current tunnel config and backup are inspected.
Verify the tunnel route and Access policy from outside the LAN. Do not expose
port 16680 directly or create router port forwards.

## Rollback

Remove only the public hostname route from the named Cloudflare Tunnel to
revoke public access. To remove the in-cluster application, delete its
HTTPRoute, Service, and Deployment. Preserve the PVC and configuration until
a backup has been verified; local-path reclaim behavior may remove data when
the PVC is deleted.

## Source

- https://github.com/wangwanjie/gately
- README quick start, security options, and Docker image architecture support.
