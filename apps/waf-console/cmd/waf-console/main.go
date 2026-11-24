package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/api"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/argocd"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/blocker"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/config"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/gitops"
	"github.com/kbsonlong/homelab-k8s/apps/waf-console/internal/logs"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg, err := config.Load()
	if err != nil {
		log.Error("load config", "err", err)
		os.Exit(1)
	}

	git := gitops.NewGitea(cfg.Git)
	vl := logs.New(cfg.VictoriaLogsURL, cfg.LogStream)
	ac := argocd.New(cfg.ArgoCD)
	allow, err := blocker.LoadAllowlist(cfg.AllowlistFile)
	if err != nil {
		log.Error("load allowlist", "err", err)
		os.Exit(1)
	}
	var enf blocker.Enforcer
	switch cfg.Blocker.Backend {
	case "ipset":
		enf = blocker.NewIPSetClient(cfg.Blocker)
	default: // gateway：Cilium Gateway（Envoy）层按 CF-Connecting-IP 封禁
		ge, err := blocker.NewGatewayRouteEnforcer(cfg.Blocker, log)
		if err != nil {
			log.Error("gateway enforcer", "err", err)
			os.Exit(1)
		}
		enf = ge
		go ge.Reconcile(context.Background(), 30*time.Second)
	}
	bl := blocker.NewManager(enf, allow, cfg.Blocker, git, log)

	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           api.New(cfg, git, vl, ac, bl, log).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go bl.Run(ctx) // 定期把封禁记录批量提交到 Git

	go func() {
		log.Info("waf-console listening", "addr", cfg.Listen)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("server", "err", err)
			stop()
		}
	}()
	<-ctx.Done()
	shut, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shut)
}
