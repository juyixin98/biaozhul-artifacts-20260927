// Command server 启动本地滚动发布控制器服务。
//
// 所有状态都落在 --data 目录：SQLite 数据库 + 模拟进程管理器状态文件。
// 不访问任何外部账号或真实业务系统。
package main

import (
	"context"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"rollingdeploy/internal/adapters/procmanager"
	"rollingdeploy/internal/config"
	"rollingdeploy/internal/controller"
	"rollingdeploy/internal/httpserver"
	"rollingdeploy/internal/procman"
	"rollingdeploy/internal/service"
	"rollingdeploy/internal/store"
)

func main() {
	cfgPath := flag.String("config", "configs/demo.json", "path to JSON config")
	dataDir := flag.String("data", "", "override data_dir")
	addr := flag.String("addr", "", "override http_addr")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		slog.Error("load config", "err", err)
		os.Exit(1)
	}
	if *dataDir != "" {
		cfg.DataDir = *dataDir
	}
	if *addr != "" {
		cfg.HTTPAddr = *addr
	}
	if err := os.MkdirAll(cfg.DataDir, 0o755); err != nil {
		slog.Error("create data dir", "dir", cfg.DataDir, "err", err)
		os.Exit(1)
	}

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	st, err := store.Open(filepath.Join(cfg.DataDir, "controller.db"))
	if err != nil {
		logger.Error("open store", "err", err)
		os.Exit(1)
	}
	defer st.Close()

	pm, err := procman.New(filepath.Join(cfg.DataDir, "procman.json"), cfg.Fixture)
	if err != nil {
		logger.Error("init procman", "err", err)
		os.Exit(1)
	}

	svc := &service.Service{
		St: st,
		Defaults: service.Defaults{
			MaxSurge: cfg.DefaultMaxSurge, MaxUnavailable: cfg.DefaultMaxUnavailable,
			ReadyThreshold: cfg.DefaultReadyThreshold, FailureLimit: cfg.DefaultFailureLimit,
			ProgressTicks: cfg.DefaultProgressTicks,
		},
	}
	ctrl := controller.New(
		func(ctx context.Context, fn func(controller.TxFace) error) error {
			return st.WithTx(ctx, func(tx store.Tx) error { return fn(tx) })
		},
		procmanager.New(pm), logger)
	srv := &httpserver.Server{
		St: st, Svc: svc, Ctrl: ctrl, PM: pm, Log: logger,
		AutoRollback: cfg.AutoRollbackOnFailure,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go srv.RunBackground(ctx, cfg.TickInterval.Duration)

	httpSrv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           srv.Router(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Info("server listening", "addr", cfg.HTTPAddr, "data", cfg.DataDir,
			"tick", cfg.TickInterval.String(), "auto_rollback", cfg.AutoRollbackOnFailure)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("http server", "err", err)
			stop()
		}
	}()
	<-ctx.Done()
	shCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shCtx)
	logger.Info("server stopped")
}
