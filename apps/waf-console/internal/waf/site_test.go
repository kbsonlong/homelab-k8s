package waf

import (
	"strings"
	"testing"
)

func TestRenderBeforeCRS(t *testing.T) {
	s := Site{Name: "gitea", Host: "gitea.kbsonlong.com", Mode: ModeMirror, MirrorPercent: 100, Paranoia: 2,
		SkipPaths:  []string{`\.git/git-receive-pack$`},
		Exclusions: []Exclusion{{URI: "/api/v1/repos/", Rules: []int{942100}, Target: "ARGS_JSON", Reason: "code"}}}
	s.Anomaly.Inbound, s.Anomaly.Outbound = 5, 4
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	out := s.RenderBeforeCRS(1)
	for _, want := range []string{"id:1001000", "tx.blocking_paranoia_level=2", "ctl:ruleEngine=Off", "ctl:ruleRemoveTargetById=942100;ARGS_JSON", "-shadow"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
}

func TestValidateRejectsInjection(t *testing.T) {
	s := Site{Name: "x", Host: "x", Mode: ModeMirror, Paranoia: 1,
		Exclusions: []Exclusion{{URI: "/a\"\nSecRuleEngine Off", Rules: []int{1}}}}
	if s.Validate() == nil {
		t.Fatal("expected error")
	}
}
