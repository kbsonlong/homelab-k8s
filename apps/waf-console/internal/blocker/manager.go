package blocker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/netip"
	"sort"
	"sync"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/config"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/gitops"
)

// Enforcer 是执行点：VM100 ipset（默认）或 Cilium 策略。
type Enforcer interface {
	Add(ctx context.Context, ip netip.Addr, ttl time.Duration) error
	Del(ctx context.Context, ip netip.Addr) error
	List(ctx context.Context) (map[string]time.Duration, error) // ip -> 剩余 TTL
}

type Block struct {
	IP      string    `json:"ip" yaml:"ip"`
	Reason  string    `json:"reason" yaml:"reason"`
	Source  string    `json:"source" yaml:"source"` // auto|manual
	By      string    `json:"by" yaml:"by"`
	Created time.Time `json:"created" yaml:"created"`
	Expires time.Time `json:"expires" yaml:"expires"`
	Action  string    `json:"action" yaml:"action"` // block|unblock
}

var (
	ErrAllowlisted = errors.New("ip is allowlisted")
	ErrPaused      = errors.New("auto block is paused")
	ErrLimit       = errors.New("block list is full")
)

type Manager struct {
	enf   Enforcer
	allow *Allowlist
	cfg   config.Blocker
	git   gitops.Repo
	log   *slog.Logger

	mu      sync.Mutex
	auto    bool
	active  map[string]Block
	pending []Block // 待写入 Git 的审计记录
	counts  map[string][]time.Time
}

func NewManager(e Enforcer, a *Allowlist, c config.Blocker, g gitops.Repo, l *slog.Logger) *Manager {
	return &Manager{enf: e, allow: a, cfg: c, git: g, log: l, auto: c.AutoEnabled,
		active: map[string]Block{}, counts: map[string][]time.Time{}}
}

func (m *Manager) SetAuto(on bool) { m.mu.Lock(); m.auto = on; m.mu.Unlock() }
func (m *Manager) Auto() bool      { m.mu.Lock(); defer m.mu.Unlock(); return m.auto }

// Block 封禁；source=auto 时受自动开关控制，并按 24h 内重复次数升级 TTL。
func (m *Manager) Block(ctx context.Context, ipStr, reason, source, by string, ttl time.Duration) (*Block, error) {
	ip, err := netip.ParseAddr(ipStr)
	if err != nil {
		return nil, fmt.Errorf("invalid ip: %w", err)
	}
	ip = ip.Unmap()
	if m.allow.Contains(ip) {
		return nil, ErrAllowlisted
	}
	m.mu.Lock()
	if source == "auto" && !m.auto {
		m.mu.Unlock()
		return nil, ErrPaused
	}
	if _, exists := m.active[ip.String()]; !exists && len(m.active) >= m.cfg.MaxEntries {
		m.mu.Unlock()
		return nil, ErrLimit
	}
	if ttl <= 0 {
		ttl = m.cfg.DefaultTTL
	}
	if source == "auto" && m.repeat(ip.String()) >= 3 {
		ttl = m.cfg.MaxTTL // WafRepeatOffender
	}
	if ttl > m.cfg.MaxTTL {
		ttl = m.cfg.MaxTTL
	}
	m.mu.Unlock()

	if err := m.enf.Add(ctx, ip, ttl); err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	b := Block{IP: ip.String(), Reason: reason, Source: source, By: by, Created: now, Expires: now.Add(ttl), Action: "block"}
	m.mu.Lock()
	m.active[b.IP] = b
	m.pending = append(m.pending, b)
	m.mu.Unlock()
	m.log.Info("blocked", "ip", b.IP, "ttl", ttl, "reason", reason, "source", source)
	return &b, nil
}

func (m *Manager) Unblock(ctx context.Context, ipStr, by string) error {
	ip, err := netip.ParseAddr(ipStr)
	if err != nil {
		return err
	}
	if err := m.enf.Del(ctx, ip.Unmap()); err != nil {
		return err
	}
	m.mu.Lock()
	delete(m.active, ip.String())
	m.pending = append(m.pending, Block{IP: ip.String(), By: by, Created: time.Now().UTC(), Action: "unblock"})
	m.mu.Unlock()
	return nil
}

// List 以执行点为准（内核 TTL 到期会自动清除），合并本地元数据。
func (m *Manager) List(ctx context.Context) ([]Block, error) {
	live, err := m.enf.List(ctx)
	if err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]Block, 0, len(live))
	for ip, left := range live {
		b, ok := m.active[ip]
		if !ok {
			b = Block{IP: ip, Source: "unknown"}
		}
		b.Expires = time.Now().UTC().Add(left)
		out = append(out, b)
	}
	for ip := range m.active { // 已过期
		if _, ok := live[ip]; !ok {
			delete(m.active, ip)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Expires.After(out[j].Expires) })
	return out, nil
}

func (m *Manager) repeat(ip string) int {
	cut := time.Now().Add(-24 * time.Hour)
	ts := m.counts[ip][:0]
	for _, t := range m.counts[ip] {
		if t.After(cut) {
			ts = append(ts, t)
		}
	}
	ts = append(ts, time.Now())
	m.counts[ip] = ts
	return len(ts) - 1
}

// Run 定期把封禁审计记录追加到 config/waf/blocklist.yaml。
func (m *Manager) Run(ctx context.Context) {
	t := time.NewTicker(m.cfg.FlushEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			m.flush(context.Background())
			return
		case <-t.C:
			m.flush(ctx)
		}
	}
}

func (m *Manager) flush(ctx context.Context) {
	m.mu.Lock()
	batch := m.pending
	m.pending = nil
	m.mu.Unlock()
	if len(batch) == 0 {
		return
	}
	file := m.git.ConfigDir() + "/blocklist.yaml"
	var doc struct {
		Records []Block `yaml:"records"`
	}
	if b, _, err := m.git.Get(ctx, file); err == nil {
		_ = yaml.Unmarshal(b, &doc)
	}
	doc.Records = append(doc.Records, batch...)
	if n := len(doc.Records); n > 5000 { // 只保留最近 5000 条
		doc.Records = doc.Records[n-5000:]
	}
	out, _ := yaml.Marshal(doc)
	if _, err := m.git.Commit(ctx, fmt.Sprintf("waf: record %d block events", len(batch)), map[string][]byte{file: out}); err != nil {
		m.log.Error("flush blocklist", "err", err)
		m.mu.Lock()
		m.pending = append(batch, m.pending...)
		m.mu.Unlock()
	}
}
