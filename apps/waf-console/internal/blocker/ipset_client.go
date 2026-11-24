package blocker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/netip"
	"time"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/config"
)

// IPSetClient 调用 VM100 上的 waf-blocker agent（见 cmd/waf-blocker）。
type IPSetClient struct {
	url, token string
	hc         *http.Client
}

func NewIPSetClient(c config.Blocker) *IPSetClient {
	return &IPSetClient{url: c.URL, token: c.Token, hc: &http.Client{Timeout: 5 * time.Second}}
}

func (c *IPSetClient) Add(ctx context.Context, ip netip.Addr, ttl time.Duration) error {
	return c.call(ctx, http.MethodPost, map[string]any{"ip": ip.String(), "ttl": int(ttl.Seconds())}, nil)
}

func (c *IPSetClient) Del(ctx context.Context, ip netip.Addr) error {
	return c.call(ctx, http.MethodDelete, map[string]any{"ip": ip.String()}, nil)
}

func (c *IPSetClient) List(ctx context.Context) (map[string]time.Duration, error) {
	var out map[string]int
	if err := c.call(ctx, http.MethodGet, nil, &out); err != nil {
		return nil, err
	}
	res := make(map[string]time.Duration, len(out))
	for ip, s := range out {
		res[ip] = time.Duration(s) * time.Second
	}
	return res, nil
}

func (c *IPSetClient) call(ctx context.Context, method string, body, out any) error {
	var buf bytes.Buffer
	if body != nil {
		_ = json.NewEncoder(&buf).Encode(body)
	}
	req, _ := http.NewRequestWithContext(ctx, method, c.url+"/v1/blocks", &buf)
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("waf-blocker: %s", resp.Status)
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}
