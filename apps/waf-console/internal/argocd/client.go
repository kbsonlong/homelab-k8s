package argocd

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/config"
)

type Client struct {
	cfg config.ArgoCD
	hc  *http.Client
}

func New(c config.ArgoCD) *Client {
	tr := &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: c.Insecure}} //nolint:gosec // 集群内自签
	return &Client{cfg: c, hc: &http.Client{Timeout: 10 * time.Second, Transport: tr}}
}

type Status struct {
	Sync     string `json:"sync"`
	Health   string `json:"health"`
	Revision string `json:"revision"`
}

func (c *Client) Status(ctx context.Context) (*Status, error) {
	var out struct {
		Status struct {
			Sync   struct{ Status, Revision string } `json:"sync"`
			Health struct{ Status string }           `json:"health"`
		} `json:"status"`
	}
	if err := c.call(ctx, http.MethodGet, "/api/v1/applications/"+c.cfg.App, &out); err != nil {
		return nil, err
	}
	return &Status{out.Status.Sync.Status, out.Status.Health.Status, out.Status.Sync.Revision}, nil
}

func (c *Client) Sync(ctx context.Context) error {
	return c.call(ctx, http.MethodPost, "/api/v1/applications/"+c.cfg.App+"/sync", nil)
}

func (c *Client) call(ctx context.Context, method, p string, out any) error {
	req, _ := http.NewRequestWithContext(ctx, method, c.cfg.BaseURL+p, nil)
	req.Header.Set("Authorization", "Bearer "+c.cfg.Token)
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("argocd %s %s: %s", method, p, resp.Status)
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}
