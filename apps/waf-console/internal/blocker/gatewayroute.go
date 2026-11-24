package blocker

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/netip"
	"os"
	"sort"
	"sync"
	"time"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/config"
)

// GatewayRouteEnforcer 在 Cilium Gateway（Envoy）层封禁真实客户端 IP。
//
// 外网流量经 Cloudflare Tunnel 进入，L3 源地址恒为 cloudflared（192.168.3.18），
// 因此不能用 CIDR/网络策略封禁，只能按 Cloudflare 透传的 CF-Connecting-IP 头匹配。
//
// 实现只用 Gateway API 标准字段（不碰 Cilium 生成的 Envoy listener）：
// 维护一条 HTTPRoute waf/waf-blocklist，hostnames 覆盖受保护站点，
// 每个被封 IP 是一个 match（PathPrefix / + Header CF-Connecting-IP Exact <ip>），
// backend 指向 crs-detector:8082（固定返回 403）。
// 按 Gateway API 路由优先级，“相同路径下 header match 更多”的规则优先，
// 所以被封 IP 的请求会命中本路由，其余请求继续走原 HTTPRoute。
//
// 过期时间存在 annotation 里，重启不丢；Reconcile 定期清理过期条目。
type GatewayRouteEnforcer struct {
	cfg        config.Blocker
	api, token string
	hc         *http.Client
	log        *slog.Logger
	mu         sync.Mutex
}

const (
	saDir             = "/var/run/secrets/kubernetes.io/serviceaccount"
	expiryAnn         = "waf.kbsonlong.com/expires" // JSON: {"1.2.3.4": "2026-11-24T12:00:00Z"}
	sentinelHeader    = "X-Waf-Blocklist-Sentinel"   // 名单为空时的占位 match（rules 不能为空，否则会匹配全部请求）
	sentinelValue     = "never-match-0b7c1e"
	maxMatchesPerRule = 64 // Gateway API v1 限制
	maxRules          = 16
)

var errNotFound = errors.New("not found")

func NewGatewayRouteEnforcer(c config.Blocker, log *slog.Logger) (*GatewayRouteEnforcer, error) {
	if len(c.Hostnames) == 0 {
		return nil, fmt.Errorf("BLOCK_HOSTNAMES is empty")
	}
	tok, err := os.ReadFile(saDir + "/token")
	if err != nil {
		return nil, err
	}
	ca, err := os.ReadFile(saDir + "/ca.crt")
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	host := "https://" + os.Getenv("KUBERNETES_SERVICE_HOST") + ":" + os.Getenv("KUBERNETES_SERVICE_PORT")
	return &GatewayRouteEnforcer{
		cfg: c, api: host, token: string(tok), log: log,
		hc: &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}},
	}, nil
}

func (g *GatewayRouteEnforcer) collURL() string {
	return fmt.Sprintf("%s/apis/gateway.networking.k8s.io/v1/namespaces/%s/httproutes", g.api, g.cfg.RouteNamespace)
}

func (g *GatewayRouteEnforcer) itemURL() string { return g.collURL() + "/" + g.cfg.RouteName }

func (g *GatewayRouteEnforcer) do(ctx context.Context, method, url string, body, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+g.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := g.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return errNotFound
	}
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("%s %s: %s %s", method, url, resp.Status, b)
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

func (g *GatewayRouteEnforcer) get(ctx context.Context) (map[string]time.Time, string, error) {
	var obj struct {
		Metadata struct {
			ResourceVersion string            `json:"resourceVersion"`
			Annotations     map[string]string `json:"annotations"`
		} `json:"metadata"`
	}
	err := g.do(ctx, http.MethodGet, g.itemURL(), nil, &obj)
	if errors.Is(err, errNotFound) {
		return map[string]time.Time{}, "", nil
	}
	if err != nil {
		return nil, "", err
	}
	raw := map[string]string{}
	if a := obj.Metadata.Annotations[expiryAnn]; a != "" {
		if err := json.Unmarshal([]byte(a), &raw); err != nil {
			return nil, "", fmt.Errorf("bad %s annotation: %w", expiryAnn, err)
		}
	}
	m := make(map[string]time.Time, len(raw))
	for ip, s := range raw {
		if t, err := time.Parse(time.RFC3339, s); err == nil {
			m[ip] = t
		}
	}
	return m, obj.Metadata.ResourceVersion, nil
}

func (g *GatewayRouteEnforcer) build(m map[string]time.Time, rv string) (map[string]any, error) {
	if len(m) > maxMatchesPerRule*maxRules {
		return nil, fmt.Errorf("blocklist too large: %d > %d", len(m), maxMatchesPerRule*maxRules)
	}
	ips := make([]string, 0, len(m))
	ann := make(map[string]string, len(m))
	for ip, t := range m {
		ips = append(ips, ip)
		ann[ip] = t.UTC().Format(time.RFC3339)
	}
	sort.Strings(ips)

	backend := []map[string]any{{"name": g.cfg.DenyService, "port": g.cfg.DenyPort}}
	match := func(name, value string) map[string]any {
		return map[string]any{
			"path":    map[string]any{"type": "PathPrefix", "value": "/"},
			"headers": []map[string]any{{"type": "Exact", "name": name, "value": value}},
		}
	}
	var rules []map[string]any
	if len(ips) == 0 {
		rules = append(rules, map[string]any{"matches": []map[string]any{match(sentinelHeader, sentinelValue)}, "backendRefs": backend})
	}
	for i := 0; i < len(ips); i += maxMatchesPerRule {
		end := min(i+maxMatchesPerRule, len(ips))
		ms := make([]map[string]any, 0, end-i)
		for _, ip := range ips[i:end] {
			ms = append(ms, match(g.cfg.Header, ip))
		}
		rules = append(rules, map[string]any{"matches": ms, "backendRefs": backend})
	}

	annJSON, err := json.Marshal(ann)
	if err != nil {
		return nil, err
	}
	meta := map[string]any{
		"name":        g.cfg.RouteName,
		"namespace":   g.cfg.RouteNamespace,
		"labels":      map[string]string{"app.kubernetes.io/managed-by": "waf-console", "app.kubernetes.io/part-of": "waf"},
		"annotations": map[string]string{expiryAnn: string(annJSON)},
	}
	if rv != "" {
		meta["resourceVersion"] = rv
	}
	parent := map[string]any{"name": g.cfg.ParentName, "namespace": g.cfg.ParentNamespace}
	if g.cfg.ParentSection != "" {
		parent["sectionName"] = g.cfg.ParentSection
	}
	return map[string]any{
		"apiVersion": "gateway.networking.k8s.io/v1",
		"kind":       "HTTPRoute",
		"metadata":   meta,
		"spec": map[string]any{
			"parentRefs": []map[string]any{parent},
			"hostnames":  g.cfg.Hostnames,
			"rules":      rules,
		},
	}, nil
}

// update 读-改-写，PUT 带 resourceVersion（乐观锁），冲突时重试。
func (g *GatewayRouteEnforcer) update(ctx context.Context, fn func(map[string]time.Time)) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	var lastErr error
	for i := 0; i < 3; i++ {
		m, rv, err := g.get(ctx)
		if err != nil {
			return err
		}
		fn(m)
		obj, err := g.build(m, rv)
		if err != nil {
			return err
		}
		if rv == "" {
			err = g.do(ctx, http.MethodPost, g.collURL(), obj, nil)
		} else {
			err = g.do(ctx, http.MethodPut, g.itemURL(), obj, nil)
		}
		if err == nil {
			return nil
		}
		lastErr = err
		g.log.Warn("blocklist route update retry", "attempt", i+1, "err", err)
	}
	return fmt.Errorf("update blocklist route: %w", lastErr)
}

func (g *GatewayRouteEnforcer) Add(ctx context.Context, ip netip.Addr, ttl time.Duration) error {
	return g.update(ctx, func(m map[string]time.Time) { m[ip.String()] = time.Now().Add(ttl) })
}

func (g *GatewayRouteEnforcer) Del(ctx context.Context, ip netip.Addr) error {
	return g.update(ctx, func(m map[string]time.Time) { delete(m, ip.String()) })
}

func (g *GatewayRouteEnforcer) List(ctx context.Context) (map[string]time.Duration, error) {
	m, _, err := g.get(ctx)
	if err != nil {
		return nil, err
	}
	out := make(map[string]time.Duration, len(m))
	for ip, t := range m {
		if left := time.Until(t); left > 0 {
			out[ip] = left
		}
	}
	return out, nil
}

// Reconcile 定期移除过期条目，并在路由不存在时创建占位路由。
func (g *GatewayRouteEnforcer) Reconcile(ctx context.Context, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		n := 0
		err := g.update(ctx, func(m map[string]time.Time) {
			for ip, exp := range m {
				if time.Now().After(exp) {
					delete(m, ip)
					n++
				}
			}
		})
		if err != nil {
			g.log.Error("reconcile blocklist route", "err", err)
		} else if n > 0 {
			g.log.Info("expired blocks removed", "count", n)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}
