package blocker

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/netip"
	"os"
	"sort"
	"sync"
	"time"
)

// CiliumEnforcer 在 Gateway 层封禁：维护一个 CiliumCIDRGroup，
// 由静态的 CiliumClusterwideNetworkPolicy（选中 reserved:ingress）通过 cidrGroupRef 引用并 ingressDeny。
// 过期时间保存在 CIDRGroup 的 annotation 中，因此 waf-console 重启后不会丢状态；Reconcile 定期清理过期条目。
type CiliumEnforcer struct {
	api, token, group, apiVersion string
	hc                            *http.Client
	log                           *slog.Logger
	mu                            sync.Mutex
}

const (
	saDir     = "/var/run/secrets/kubernetes.io/serviceaccount"
	expiryAnn = "waf.kbsonlong.com/expires" // JSON: {"1.2.3.4/32": "2026-11-24T12:00:00Z"}
)

// apiVersion: cilium.io/v2alpha1（Cilium 1.20 若已升级到 v2，改这里）
func NewCiliumEnforcer(group, apiVersion string, log *slog.Logger) (*CiliumEnforcer, error) {
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
	return &CiliumEnforcer{api: host, token: string(tok), group: group, apiVersion: apiVersion, log: log,
		hc: &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}}}, nil
}

type cidrGroup struct {
	Metadata struct {
		Annotations     map[string]string `json:"annotations,omitempty"`
		ResourceVersion string            `json:"resourceVersion,omitempty"`
	} `json:"metadata"`
	Spec struct {
		ExternalCIDRs []string `json:"externalCIDRs"`
	} `json:"spec"`
}

func (c *CiliumEnforcer) url() string {
	return fmt.Sprintf("%s/apis/%s/ciliumcidrgroups/%s", c.api, c.apiVersion, c.group)
}

func (c *CiliumEnforcer) get(ctx context.Context) (map[string]time.Time, string, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.url(), nil)
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, "", fmt.Errorf("get cidrgroup: %s", resp.Status)
	}
	var g cidrGroup
	if err := json.NewDecoder(resp.Body).Decode(&g); err != nil {
		return nil, "", err
	}
	exp := map[string]time.Time{}
	_ = json.Unmarshal([]byte(g.Metadata.Annotations[expiryAnn]), &exp)
	m := map[string]time.Time{}
	for _, cidr := range g.Spec.ExternalCIDRs {
		t, ok := exp[cidr]
		if !ok {
			t = time.Now().Add(100 * 365 * 24 * time.Hour) // 手工添加、无过期时间 = 永久
		}
		m[cidr] = t
	}
	return m, g.Metadata.ResourceVersion, nil
}

// put 用 merge patch 一次性写入 CIDR 列表和过期 annotation；带 resourceVersion 做乐观锁。
func (c *CiliumEnforcer) put(ctx context.Context, m map[string]time.Time, rv string) error {
	cidrs := make([]string, 0, len(m))
	exp := map[string]string{}
	for k, t := range m {
		cidrs = append(cidrs, k)
		exp[k] = t.UTC().Format(time.RFC3339)
	}
	sort.Strings(cidrs)
	ann, _ := json.Marshal(exp)
	var g cidrGroup
	g.Metadata.ResourceVersion = rv
	g.Metadata.Annotations = map[string]string{expiryAnn: string(ann)}
	g.Spec.ExternalCIDRs = cidrs
	body, _ := json.Marshal(g)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPatch, c.url(), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/merge-patch+json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("patch cidrgroup: %s", resp.Status)
	}
	return nil
}

func (c *CiliumEnforcer) update(ctx context.Context, fn func(map[string]time.Time)) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := 0; i < 3; i++ { // 409 冲突重试
		m, rv, err := c.get(ctx)
		if err != nil {
			return err
		}
		fn(m)
		if err = c.put(ctx, m, rv); err == nil {
			return nil
		}
		c.log.Warn("cidrgroup update retry", "err", err)
	}
	return fmt.Errorf("cidrgroup update failed after retries")
}

func prefix(ip netip.Addr) string { return netip.PrefixFrom(ip, ip.BitLen()).String() } // /32 或 /128，IPv6 也支持

func (c *CiliumEnforcer) Add(ctx context.Context, ip netip.Addr, ttl time.Duration) error {
	return c.update(ctx, func(m map[string]time.Time) { m[prefix(ip)] = time.Now().Add(ttl) })
}

func (c *CiliumEnforcer) Del(ctx context.Context, ip netip.Addr) error {
	return c.update(ctx, func(m map[string]time.Time) { delete(m, prefix(ip)) })
}

func (c *CiliumEnforcer) List(ctx context.Context) (map[string]time.Duration, error) {
	m, _, err := c.get(ctx)
	if err != nil {
		return nil, err
	}
	out := map[string]time.Duration{}
	for k, t := range m {
		if p, err := netip.ParsePrefix(k); err == nil && time.Until(t) > 0 {
			out[p.Addr().String()] = time.Until(t)
		}
	}
	return out, nil
}

// Reconcile 定期移除过期条目（ipset 由内核负责 TTL，Cilium 需要自己做）。
func (c *CiliumEnforcer) Reconcile(ctx context.Context, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			n := 0
			err := c.update(ctx, func(m map[string]time.Time) {
				for k, exp := range m {
					if time.Now().After(exp) {
						delete(m, k)
						n++
					}
				}
			})
			if err != nil {
				c.log.Error("reconcile cidrgroup", "err", err)
			} else if n > 0 {
				c.log.Info("expired blocks removed", "count", n)
			}
		}
	}
}
