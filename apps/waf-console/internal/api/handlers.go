package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"path"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/logs"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/waf"
)

// ---------- 站点 ----------

func (s *Server) sitesDir() string { return path.Join(s.git.ConfigDir(), "sites") }

func (s *Server) loadSite(ctx context.Context, name string) (*waf.Site, error) {
	b, _, err := s.git.Get(ctx, path.Join(s.sitesDir(), name, "site.yaml"))
	if err != nil {
		return nil, err
	}
	var st waf.Site
	return &st, yaml.Unmarshal(b, &st)
}

func (s *Server) loadSites(ctx context.Context) ([]*waf.Site, error) {
	names, err := s.git.List(ctx, s.sitesDir())
	if err != nil {
		return nil, err
	}
	out := make([]*waf.Site, 0, len(names))
	for _, n := range names {
		st, err := s.loadSite(ctx, n)
		if err != nil {
			s.log.Warn("load site", "site", n, "err", err)
			continue
		}
		out = append(out, st)
	}
	return out, nil
}

func (s *Server) listSites(w http.ResponseWriter, r *http.Request) {
	sites, err := s.loadSites(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, sites)
}

func (s *Server) getSite(w http.ResponseWriter, r *http.Request) {
	st, err := s.loadSite(r.Context(), r.PathValue("name"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) putSite(w http.ResponseWriter, r *http.Request) {
	var st waf.Site
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&st); err != nil {
		writeErr(w, fmt.Errorf("%w: %v", errBadRequest, err))
		return
	}
	st.Name = r.PathValue("name")
	if err := st.Validate(); err != nil {
		writeErr(w, fmt.Errorf("%w: %v", errBadRequest, err))
		return
	}
	ref, err := s.saveSite(r.Context(), &st, fmt.Sprintf("waf(%s): update site by %s", st.Name, user(r)))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"ref": ref})
}

// saveSite 同时写 site.yaml 和渲染后的 before-crs.conf（CI 会再渲染比对一次）。
func (s *Server) saveSite(ctx context.Context, st *waf.Site, msg string) (string, error) {
	names, err := s.git.List(ctx, s.sitesDir())
	if err != nil {
		return "", err
	}
	idx := len(names)
	for i, n := range names {
		if n == st.Name {
			idx = i
		}
	}
	y, _ := yaml.Marshal(st)
	dir := path.Join(s.sitesDir(), st.Name)
	return s.git.Commit(ctx, msg, map[string][]byte{
		path.Join(dir, "site.yaml"):       y,
		path.Join(dir, "before-crs.conf"): []byte(st.RenderBeforeCRS(idx)),
	})
}

// ---------- 总览 ----------

func (s *Server) overview(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	res := map[string]any{"autoBlock": s.bl.Auto()}
	if st, err := s.argo.Status(ctx); err == nil {
		res["argocd"] = st
	} else {
		res["argocdError"] = err.Error()
	}
	if sites, err := s.loadSites(ctx); err == nil {
		res["sites"] = sites
	}
	if rows, err := s.vl.Query(ctx, "transaction.messages:* | stats count() hits, count_uniq(transaction.client_ip) ips",
		time.Now().Add(-24*time.Hour), time.Now(), 0); err == nil && len(rows) > 0 {
		res["last24h"] = rows[0]
	}
	if bl, err := s.bl.List(ctx); err == nil {
		res["activeBlocks"] = len(bl)
	}
	writeJSON(w, http.StatusOK, res)
}

// ---------- 事件 / 误报 / 学习 ----------

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	from, to := timeRange(r, time.Hour)
	q := r.URL.Query()
	evs, err := s.vl.Events(r.Context(), logs.EventFilter{
		Host: q.Get("host"), IP: q.Get("ip"), RuleID: qInt(r, "rule", 0),
		From: from, To: to, Limit: min(qInt(r, "limit", 100), 1000),
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, evs)
}

type fpReq struct {
	Site   string `json:"site"`
	RuleID int    `json:"ruleId"`
	URI    string `json:"uri"`    // 建议前缀，如 /api/v1/repos/
	Target string `json:"target"` // 如 ARGS:content；空 = 整条规则
	Reason string `json:"reason"`
	Apply  bool   `json:"apply"` // false 只预览
}

func (s *Server) falsePositive(w http.ResponseWriter, r *http.Request) {
	var req fpReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil || req.RuleID == 0 || req.URI == "" {
		writeErr(w, fmt.Errorf("%w: site, ruleId, uri required", errBadRequest))
		return
	}
	if req.RuleID >= 949000 && req.RuleID < 950000 || req.RuleID >= 980000 {
		writeErr(w, fmt.Errorf("%w: 评分汇总规则 %d 不能作为例外", errBadRequest, req.RuleID))
		return
	}
	st, err := s.loadSite(r.Context(), req.Site)
	if err != nil {
		writeErr(w, err)
		return
	}
	ex := waf.Exclusion{URI: req.URI, Rules: []int{req.RuleID}, Target: req.Target,
		Reason: req.Reason, Source: "false-positive"}
	st.Exclusions = append(st.Exclusions, ex)
	if err := st.Validate(); err != nil {
		writeErr(w, fmt.Errorf("%w: %v", errBadRequest, err))
		return
	}
	sug := waf.Suggestion{Site: st.Name, Exclusion: ex, Preview: st.RenderBeforeCRS(0)}
	if !req.Apply {
		writeJSON(w, http.StatusOK, sug)
		return
	}
	ref, err := s.saveSite(r.Context(), st, fmt.Sprintf("waf(%s): exclude %d on %s by %s", st.Name, req.RuleID, req.URI, user(r)))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"suggestion": sug, "ref": ref})
}

func (s *Server) learning(w http.ResponseWriter, r *http.Request) {
	from, to := timeRange(r, 7*24*time.Hour)
	stats, err := s.vl.Learning(r.Context(), from, to)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, stats)
}

// ---------- 封禁 ----------

func (s *Server) listBlocks(w http.ResponseWriter, r *http.Request) {
	bl, err := s.bl.List(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"auto": s.bl.Auto(), "blocks": bl})
}

func (s *Server) addBlock(w http.ResponseWriter, r *http.Request) {
	var req struct {
		IP     string `json:"ip"`
		TTL    string `json:"ttl"` // 1h / 24h
		Reason string `json:"reason"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<12)).Decode(&req); err != nil {
		writeErr(w, fmt.Errorf("%w: %v", errBadRequest, err))
		return
	}
	ttl, _ := time.ParseDuration(req.TTL)
	b, err := s.bl.Block(r.Context(), req.IP, req.Reason, "manual", user(r), ttl)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, b)
}

func (s *Server) delBlock(w http.ResponseWriter, r *http.Request) {
	if err := s.bl.Unblock(r.Context(), r.PathValue("ip"), user(r)); err != nil {
		writeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) setAuto(w http.ResponseWriter, r *http.Request) {
	var req struct{ Enabled bool }
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, fmt.Errorf("%w: %v", errBadRequest, err))
		return
	}
	s.bl.SetAuto(req.Enabled)
	s.log.Info("auto block toggled", "enabled", req.Enabled, "user", user(r))
	writeJSON(w, http.StatusOK, map[string]bool{"auto": req.Enabled})
}

// ---------- vmalert webhook（Alertmanager 兼容格式） ----------

type amPayload struct {
	Alerts []struct {
		Status      string            `json:"status"`
		Labels      map[string]string `json:"labels"`
		Annotations map[string]string `json:"annotations"`
	} `json:"alerts"`
}

// 约定 VMRule labels：client_ip、waf_action=block、waf_ttl=1h。
func (s *Server) alerts(w http.ResponseWriter, r *http.Request) {
	var p amPayload
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&p); err != nil {
		writeErr(w, fmt.Errorf("%w: %v", errBadRequest, err))
		return
	}
	var done, skipped []string
	for _, a := range p.Alerts {
		ip := a.Labels["client_ip"]
		if a.Status != "firing" || a.Labels["waf_action"] != "block" || ip == "" {
			continue
		}
		ttl, _ := time.ParseDuration(a.Labels["waf_ttl"])
		reason := a.Labels["alertname"] + ": " + a.Annotations["summary"]
		if _, err := s.bl.Block(r.Context(), ip, reason, "auto", "vmalert", ttl); err != nil {
			skipped = append(skipped, ip+"("+err.Error()+")")
			continue
		}
		done = append(done, ip)
	}
	if len(done) > 0 {
		s.notify(r.Context(), "WAF 自动封禁", "已封禁: "+strings.Join(done, ", "))
	}
	writeJSON(w, http.StatusOK, map[string]any{"blocked": done, "skipped": skipped})
}

func (s *Server) notify(ctx context.Context, title, msg string) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.NtfyURL, strings.NewReader(msg))
	req.Header.Set("Title", title)
	req.Header.Set("Tags", "shield")
	if resp, err := http.DefaultClient.Do(req); err == nil {
		resp.Body.Close()
	} else {
		s.log.Warn("ntfy", "err", err)
	}
}

// ---------- 变更 / 同步 ----------

func (s *Server) changes(w http.ResponseWriter, r *http.Request) {
	cs, err := s.git.Log(r.Context(), s.git.ConfigDir(), min(qInt(r, "limit", 30), 100))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, cs)
}

func (s *Server) sync(w http.ResponseWriter, r *http.Request) {
	if err := s.argo.Sync(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "sync requested"})
}
