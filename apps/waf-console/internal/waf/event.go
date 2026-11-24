package waf

import "time"

// Event 是从 ModSecurity JSON 审计日志中抽取的精简视图。
type Event struct {
	ID       string    `json:"id"` // transaction.unique_id
	Time     time.Time `json:"time"`
	ClientIP string    `json:"clientIp"`
	Host     string    `json:"host"`
	Method   string    `json:"method"`
	URI      string    `json:"uri"`
	Score    int       `json:"score"`
	Rules    []RuleHit `json:"rules"`
}

type RuleHit struct {
	ID       int    `json:"id"`
	Msg      string `json:"msg"`
	Severity string `json:"severity"`
	Data     string `json:"data"`  // 命中的变量，如 ARGS:q
	Match    string `json:"match"` // 已脱敏
}

// Suggestion 是“标记误报”生成的例外规则建议。
type Suggestion struct {
	Site      string    `json:"site"`
	Exclusion Exclusion `json:"exclusion"`
	Preview   string    `json:"preview"`
}
