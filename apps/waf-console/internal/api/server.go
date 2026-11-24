package api

import (
	"crypto/subtle"
	"embed"
	"encoding/json"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/argocd"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/blocker"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/config"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/gitops"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/logs"
)

//go:embed all:web
var webFS embed.FS

type Server struct {
	cfg  *config.Config
	git  gitops.Repo
	vl   *logs.Client
	argo *argocd.Client
	bl   *blocker.Manager
	log  *slog.Logger
}

func New(c *config.Config, g gitops.Repo, v *logs.Client, a *argocd.Client, b *blocker.Manager, l *slog.Logger) *Server {
	return &Server{c, g, v, a, b, l}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })

	mux.HandleFunc("GET /api/v1/overview", s.overview)
	mux.HandleFunc("GET /api/v1/sites", s.listSites)
	mux.HandleFunc("GET /api/v1/sites/{name}", s.getSite)
	mux.HandleFunc("PUT /api/v1/sites/{name}", s.putSite)
	mux.HandleFunc("GET /api/v1/events", s.events)
	mux.HandleFunc("POST /api/v1/events/false-positive", s.falsePositive)
	mux.HandleFunc("GET /api/v1/learning", s.learning)
	mux.HandleFunc("GET /api/v1/blocks", s.listBlocks)
	mux.HandleFunc("POST /api/v1/blocks", s.addBlock)
	mux.HandleFunc("DELETE /api/v1/blocks/{ip}", s.delBlock)
	mux.HandleFunc("PUT /api/v1/blocks/auto", s.setAuto)
	mux.HandleFunc("GET /api/v1/changes", s.changes)
	mux.HandleFunc("POST /api/v1/sync", s.sync)
	// vmalert webhook，使用独立 token，不走用户认证
	mux.HandleFunc("POST /api/v1/alerts", s.alerts)

	static, _ := fs.Sub(webFS, "web")
	mux.Handle("GET /", http.FileServerFS(static))

	return s.withLog(s.withAuth(mux))
}

// withAuth：/api/v1/alerts 用 ALERT_TOKEN；其余接口依赖前置的 oauth2-proxy（Gitea OAuth2）注入的用户头。
func (s *Server) withAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
		case "/api/v1/alerts":
			got := r.Header.Get("Authorization")
			want := "Bearer " + s.cfg.AlertToken
			if s.cfg.AlertToken == "" || subtle.ConstantTimeCompare([]byte(got), []byte(want)) != 1 {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
		default:
			if user(r) == "" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

func user(r *http.Request) string {
	if u := r.Header.Get("X-Forwarded-User"); u != "" {
		return u
	}
	return r.Header.Get("X-Auth-Request-User")
}

func (s *Server) withLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		s.log.Info("http", "method", r.Method, "path", r.URL.Path, "user", user(r), "dur", time.Since(start))
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	code := http.StatusInternalServerError
	var he *gitops.HTTPError
	switch {
	case errors.Is(err, errBadRequest):
		code = http.StatusBadRequest
	case errors.As(err, &he) && he.Code == http.StatusNotFound:
		code = http.StatusNotFound
	case errors.Is(err, blocker.ErrAllowlisted), errors.Is(err, blocker.ErrPaused), errors.Is(err, blocker.ErrLimit):
		code = http.StatusConflict
	}
	writeJSON(w, code, map[string]string{"error": err.Error()})
}

var errBadRequest = errors.New("bad request")

func timeRange(r *http.Request, def time.Duration) (time.Time, time.Time) {
	to := time.Now()
	from := to.Add(-def)
	if v, err := time.Parse(time.RFC3339, r.URL.Query().Get("from")); err == nil {
		from = v
	}
	if v, err := time.Parse(time.RFC3339, r.URL.Query().Get("to")); err == nil {
		to = v
	}
	return from, to
}

func qInt(r *http.Request, k string, d int) int {
	if v, err := strconv.Atoi(r.URL.Query().Get(k)); err == nil {
		return v
	}
	return d
}
