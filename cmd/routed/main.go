// Command routed 启动 RIB 服务：加载配置、打开 SQLite、从快照表恢复
// 路由、装配 HTTP 接口并开始服务。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"rib/internal/api"
	"rib/internal/config"
	"rib/internal/diag"
	"rib/internal/netmodel"
	"rib/internal/rib"
	"rib/internal/store"
)

func main() {
	cfgPath := flag.String("config", "", "path to JSON config file (optional)")
	seedPath := flag.String("seed", "", "path to a replace-style JSON seed to atomically load on startup (optional)")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "config error:", err)
		os.Exit(2)
	}
	logger := diag.NewLogger(cfg.LogLevel)
	diag.SetLogger(logger)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	st, err := store.Open(ctx, cfg.SQLiteDSN)
	if err != nil {
		logger.Error("open sqlite", "err", err)
		os.Exit(1)
	}
	defer st.Close()

	r := rib.New().WithMaxDepth(cfg.MaxDepth)
	if err := bootstrap(ctx, r, st, logger); err != nil {
		logger.Error("bootstrap from store", "err", err)
		os.Exit(1)
	}
	if *seedPath != "" {
		if err := applySeed(ctx, *seedPath, r, st, logger); err != nil {
			logger.Error("apply seed", "err", err)
			os.Exit(1)
		}
	}

	srv := &api.Server{RIB: r, Store: st, Cfg: cfg, Logger: logger}
	httpSrv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Info("listening", "addr", cfg.ListenAddr, "version", r.Version())
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http server", "err", err)
			cancel()
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")
	shutdownCtx, shCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shCancel()
	_ = httpSrv.Shutdown(shutdownCtx)
}

// applySeed 读取 replace 风格 JSON（{"v4":[...], "v6":[...]}），
// 以一次批量替换装载：先在内存整体校验+建树，再以单事务写快照与一条
// replace_all 事件，失败则进程退出而不是留下部分装载状态。
func applySeed(ctx context.Context, path string, r *rib.RIB, st *store.Store, logger interface {
	Info(msg string, args ...any)
	Error(msg string, args ...any)
}) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read seed: %w", err)
	}
	var seed struct {
		V4 []netmodel.Route `json:"v4"`
		V6 []netmodel.Route `json:"v6"`
	}
	if err := json.Unmarshal(raw, &seed); err != nil {
		return fmt.Errorf("parse seed: %w", err)
	}
	req := rib.ReplaceRequest{V4: seed.V4, V6: seed.V6}
	// ReplaceAll 内部对两族分别校验，任何非法条目都整体拒绝。
	if err := r.ReplaceAll(req); err != nil {
		return fmt.Errorf("validate seed: %w", err)
	}
	if _, err := st.CommitReplace(ctx, store.ReplacePayload{V4: seed.V4, V6: seed.V6},
		r.Version(), "startup-seed"); err != nil {
		return fmt.Errorf("persist seed: %w", err)
	}
	logger.Info("seed applied", "v4", len(seed.V4), "v6", len(seed.V6), "version", r.Version())
	return nil
}

// bootstrap 从 routes 快照装载路由；同时校验事件日志与快照一致性，
// 不一致只告警不阻断（可通过 POST /v1/replay 人工核对）。
func bootstrap(ctx context.Context, r *rib.RIB, st *store.Store, logger interface {
	Info(msg string, args ...any)
	Warn(msg string, args ...any)
}) error {
	routes, err := st.LoadRoutes(ctx)
	if err != nil {
		return fmt.Errorf("load routes: %w", err)
	}
	var v4, v6 []netmodel.Route
	for _, rt := range routes {
		switch rt.Prefix.Family() {
		case netmodel.AFIPv4:
			v4 = append(v4, rt)
		case netmodel.AFIPv6:
			v6 = append(v6, rt)
		}
	}
	if err := r.ReplaceAll(rib.ReplaceRequest{V4: v4, V6: v6}); err != nil {
		return fmt.Errorf("restore routes: %w", err)
	}
	maxSeq, err := st.MaxEventSeq(ctx)
	if err == nil {
		_ = st.SetMeta(ctx, "bootstrapped_at", time.Now().UTC().Format(time.RFC3339))
		logger.Info("bootstrap complete",
			"v4", len(v4), "v6", len(v6), "last_event_seq", maxSeq)
	}
	return nil
}
