package blocker

import (
	"fmt"
	"net/netip"
	"os"

	"gopkg.in/yaml.v3"
)

type Allowlist struct{ prefixes []netip.Prefix }

// 内置永不封禁网段，文件中的条目追加在后面。
var builtin = []string{"127.0.0.0/8", "10.0.0.0/8", "192.168.3.0/24", "100.64.0.0/10", "::1/128", "fd00::/8"}

func LoadAllowlist(file string) (*Allowlist, error) {
	a := &Allowlist{}
	cidrs := append([]string{}, builtin...)
	if b, err := os.ReadFile(file); err == nil {
		var doc struct {
			CIDRs []string `yaml:"cidrs"`
		}
		if err := yaml.Unmarshal(b, &doc); err != nil {
			return nil, fmt.Errorf("allowlist: %w", err)
		}
		cidrs = append(cidrs, doc.CIDRs...)
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	for _, c := range cidrs {
		p, err := netip.ParsePrefix(c)
		if err != nil {
			return nil, fmt.Errorf("allowlist %q: %w", c, err)
		}
		a.prefixes = append(a.prefixes, p)
	}
	return a, nil
}

func (a *Allowlist) Contains(ip netip.Addr) bool {
	for _, p := range a.prefixes {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}
