# Security policy

Do not commit `.env`, Kubernetes Secret manifests containing credentials, kubeconfig files, Cloudflare tokens, SSH keys, or private repository URLs.

Use a local password manager for `GITEA_ADMIN_PASSWORD` and `GITEA_DB_PASSWORD`. For GitHub Actions, store them as repository secrets and inject them into the deployment environment only in a controlled private runner. This public repository intentionally contains no production credentials.

Report suspected credential exposure privately to the repository owner and rotate the credential before opening a public issue.
