package logs

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"time"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/waf"
)

type EventFilter struct {
	Host, IP string
	RuleID   int
	From, To time.Time
	Limit    int
}

// 审计日志 JSON 结构（libmodsecurity v3）。字段名需在 VictoriaLogs 中实际核对。
type auditLog struct {
	Transaction struct {
		ClientIP  string `json:"client_ip"`
		TimeStamp string `json:"time_stamp"`
		UniqueID  string `json:"unique_id"`
		Request   struct {
			Method  string            `json:"method"`
			URI     string            `json:"uri"`
			Headers map[string]string `json:"headers"`
		} `json:"request"`
		Messages []struct {
			Message string `json:"message"`
			Details struct {
				RuleID   string `json:"ruleId"`
				Data     string `json:"data"`
				Match    string `json:"match"`
				Severity string `json:"severity"`
			} `json:"details"`
		} `json:"messages"`
	} `json:"transaction"`
}

var scoreRe = regexp.MustCompile(`Total Score: (\d+)`)

func (c *Client) Events(ctx context.Context, f EventFilter) ([]waf.Event, error) {
	q := "transaction.messages:*"
	if f.IP != "" {
		q += " transaction.client_ip:=" + Quote(f.IP)
	}
	if f.Host != "" {
		q += " _msg:" + Quote(f.Host)
	}
	if f.RuleID > 0 {
		q += fmt.Sprintf(` _msg:"\"ruleId\":\"%d\""`, f.RuleID)
	}
	q += " | sort by (_time desc)"
	rows, err := c.Query(ctx, q, f.From, f.To, f.Limit)
	if err != nil {
		return nil, err
	}
	out := make([]waf.Event, 0, len(rows))
	for _, r := range rows {
		if ev, ok := parse(r); ok {
			out = append(out, ev)
		}
	}
	return out, nil
}

func parse(r map[string]any) (waf.Event, bool) {
	msg, _ := r["_msg"].(string)
	var a auditLog
	if json.Unmarshal([]byte(msg), &a) != nil {
		return waf.Event{}, false
	}
	t := a.Transaction
	ts, _ := time.Parse(time.RFC3339Nano, fmt.Sprint(r["_time"]))
	ev := waf.Event{ID: t.UniqueID, Time: ts, ClientIP: t.ClientIP, Method: t.Request.Method, URI: t.Request.URI,
		Host: t.Request.Headers["Host"]}
	for _, m := range t.Messages {
		id, _ := strconv.Atoi(m.Details.RuleID)
		if s := scoreRe.FindStringSubmatch(m.Details.Data); s != nil {
			ev.Score, _ = strconv.Atoi(s[1])
		}
		ev.Rules = append(ev.Rules, waf.RuleHit{ID: id, Msg: m.Message, Severity: m.Details.Severity,
			Data: m.Details.Data, Match: redact(m.Details.Match)})
	}
	return ev, true
}

var secretRe = regexp.MustCompile(`(?i)(password|passwd|token|secret|authorization|cookie)=([^&\s]+)`)

func redact(s string) string { return secretRe.ReplaceAllString(s, "$1=***") }

type RuleStat struct {
	RuleID int    `json:"ruleId"`
	URI    string `json:"uri"`
	Hits   int    `json:"hits"`
	IPs    int    `json:"distinctIps"`
	FPRisk string `json:"fpRisk"` // high|medium|low
}

// Learning 学习报告：规则 × URI 聚合，“高频 + 来源分散”优先作为误报候选。
func (c *Client) Learning(ctx context.Context, from, to time.Time) ([]RuleStat, error) {
	q := `transaction.messages:* | unpack_json fields (transaction) | unpack_json from transaction fields (client_ip, request, messages)` +
		` | unpack_json from request fields (uri) | unroll (messages) | unpack_json from messages fields (details)` +
		` | unpack_json from details fields (ruleId) | filter ruleId:!"949110" ruleId:!"980170"` +
		` | stats by (ruleId, uri) count() hits, count_uniq(client_ip) ips | sort by (hits desc) | limit 200`
	rows, err := c.Query(ctx, q, from, to, 0)
	if err != nil {
		return nil, err
	}
	var out []RuleStat
	for _, r := range rows {
		id, _ := strconv.Atoi(fmt.Sprint(r["ruleId"]))
		hits, _ := strconv.Atoi(fmt.Sprint(r["hits"]))
		ips, _ := strconv.Atoi(fmt.Sprint(r["ips"]))
		risk := "low"
		switch {
		case hits >= 50 && ips >= 5:
			risk = "high"
		case hits >= 10 && ips >= 2:
			risk = "medium"
		}
		out = append(out, RuleStat{id, fmt.Sprint(r["uri"]), hits, ips, risk})
	}
	return out, nil
}
