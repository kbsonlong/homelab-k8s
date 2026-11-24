package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Git struct {
	BaseURL      string // https://gitea.kbsonlong.com
	Owner, Repo  string // kbsonlong / homelab-k8s
	Branch       string // main
	Token        string
	ConfigDir    string // config/waf
	DirectCommit bool   // true: 直接提交 main；false: 新分支 + PR
}

type ArgoCD struct {
	BaseURL, Token, App string
	Insecure            bool
}

type Blocker struct {
	Backend string // gateway(默认) | ipset

	// gateway：在 Cilium Gateway（Envoy）层按 Cloudflare 透传的客户端 IP 头封禁
	Header          string   // CF-Connecting-IP
	Hostnames       []string // 受保护站点
	RouteNamespace  string   // waf
	RouteName       string   // waf-blocklist
	ParentName      string   // lab-gateway
	ParentNamespace string   // cilium-gateway
	ParentSection   string   // http
	DenyService     string   // crs-detector（8082 固定返回 403）
	DenyPort        int      // 8082

	URL, Token string // ipset 模式下的 waf-blocker

	DefaultTTL  time.Duration // 1h
	MaxTTL      time.Duration // 168h
	MaxEntries  int           // 1000（Gateway API 上限 16 rules × 64 matches）
	AutoEnabled bool
	FlushEvery  time.Duration // 封禁记录写 Git 间隔
}

type Config struct {
	Listen          string
	VictoriaLogsURL string
	LogStream       string
	AllowlistFile   string
	NtfyURL         string
	AlertToken      string // vmalert webhook 共享 token
	Git             Git
	ArgoCD          ArgoCD
	Blocker         Blocker
}

func Load() (*Config, error) {
	c := &Config{
		Listen:          env("LISTEN", ":8080"),
		VictoriaLogsURL: env("VLOGS_URL", "http://vlsingle-vmk8s-victoria-metrics-k8s-stack.monitoring.svc:9428"),
		LogStream:       env("VLOGS_STREAM", `{kubernetes.namespace="waf", kubernetes.container_name="crs-detector"}`),
		AllowlistFile:   env("ALLOWLIST_FILE", "/etc/waf-console/allowlist.yaml"),
		NtfyURL:         env("NTFY_URL", "http://ntfy.ntfy.svc/waf"),
		AlertToken:      os.Getenv("ALERT_TOKEN"),
		Git: Git{
			BaseURL:      env("GIT_URL", "https://gitea.kbsonlong.com"),
			Owner:        env("GIT_OWNER", "kbsonlong"),
			Repo:         env("GIT_REPO", "homelab-k8s"),
			Branch:       env("GIT_BRANCH", "main"),
			Token:        os.Getenv("GIT_TOKEN"),
			ConfigDir:    env("GIT_CONFIG_DIR", "config/waf"),
			DirectCommit: envBool("GIT_DIRECT_COMMIT", true),
		},
		ArgoCD: ArgoCD{
			BaseURL:  env("ARGOCD_URL", "http://argo-cd-argocd-server.argocd.svc"),
			Token:    os.Getenv("ARGOCD_TOKEN"),
			App:      env("ARGOCD_APP", "waf"),
			Insecure: envBool("ARGOCD_INSECURE", true),
		},
		Blocker: Blocker{
			Backend:         env("BLOCK_BACKEND", "gateway"),
			Header:          env("BLOCK_HEADER", "CF-Connecting-IP"),
			Hostnames:       envList("BLOCK_HOSTNAMES", "gitea.kbsonlong.com,argocd.kbsonlong.com,grafana.kbsonlong.com"),
			RouteNamespace:  env("BLOCK_ROUTE_NAMESPACE", "waf"),
			RouteName:       env("BLOCK_ROUTE_NAME", "waf-blocklist"),
			ParentName:      env("BLOCK_GATEWAY", "lab-gateway"),
			ParentNamespace: env("BLOCK_GATEWAY_NAMESPACE", "cilium-gateway"),
			ParentSection:   env("BLOCK_GATEWAY_SECTION", "http"),
			DenyService:     env("BLOCK_DENY_SERVICE", "crs-detector"),
			DenyPort:        envInt("BLOCK_DENY_PORT", 8082),
			URL:             env("BLOCKER_URL", ""),
			Token:           os.Getenv("BLOCKER_TOKEN"),
			DefaultTTL:      envDur("BLOCK_DEFAULT_TTL", time.Hour),
			MaxTTL:          envDur("BLOCK_MAX_TTL", 7*24*time.Hour),
			MaxEntries:      envInt("BLOCK_MAX_ENTRIES", 1000),
			AutoEnabled:     envBool("BLOCK_AUTO", false),
			FlushEvery:      envDur("BLOCK_FLUSH_EVERY", 5*time.Minute),
		},
	}
	if c.Git.Token == "" {
		return nil, fmt.Errorf("GIT_TOKEN is required")
	}
	if c.Blocker.MaxEntries > 1024 {
		return nil, fmt.Errorf("BLOCK_MAX_ENTRIES must be <= 1024 (Gateway API limit)")
	}
	return c, nil
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envList(k, d string) []string {
	var out []string
	for _, s := range strings.Split(env(k, d), ",") {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}

func envBool(k string, d bool) bool {
	if b, err := strconv.ParseBool(os.Getenv(k)); err == nil {
		return b
	}
	return d
}

func envInt(k string, d int) int {
	if i, err := strconv.Atoi(os.Getenv(k)); err == nil {
		return i
	}
	return d
}

func envDur(k string, d time.Duration) time.Duration {
	if v, err := time.ParseDuration(os.Getenv(k)); err == nil {
		return v
	}
	return d
}
