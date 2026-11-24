package logs

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client 调用 VictoriaLogs 的 /select/logsql/query（返回 JSON Lines）。
type Client struct {
	base, stream string
	hc           *http.Client
}

func New(base, stream string) *Client {
	return &Client{base: strings.TrimRight(base, "/"), stream: stream, hc: &http.Client{Timeout: 30 * time.Second}}
}

// Query 执行 LogsQL，q 会自动拼上 WAF 日志流选择器。
func (c *Client) Query(ctx context.Context, q string, start, end time.Time, limit int) ([]map[string]any, error) {
	form := url.Values{}
	form.Set("query", c.stream+" "+q)
	form.Set("start", start.Format(time.RFC3339))
	form.Set("end", end.Format(time.RFC3339))
	if limit > 0 {
		form.Set("limit", fmt.Sprint(limit))
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/select/logsql/query", strings.NewReader(form.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("victorialogs: %s", resp.Status)
	}
	var rows []map[string]any
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 1<<20), 8<<20) // 审计日志单行可能很大
	for sc.Scan() {
		var m map[string]any
		if json.Unmarshal(sc.Bytes(), &m) == nil {
			rows = append(rows, m)
		}
	}
	return rows, sc.Err()
}

// Quote 用于把用户输入安全拼进 LogsQL 字符串字面量。
func Quote(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}
