// Command netpol-reach 启动离线容器网络策略可达性判定后端。
//
// 所有数据来自本地：SQLite 文件 + 可选的离线数据包 JSON 夹具。
// 启动流程：配置 -> 打开 SQLite -> 迁移 -> (可选)seed 装载 -> 初次协调 -> HTTP 服务。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"netpolreach/internal/config"
	"netpolreach/internal/diag"
	"netpolreach/internal/httpapi"
	"netpolreach/internal/ingest"
	"netpolreach/internal/reconcile"
	"netpolreach/internal/store"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "netpol-reach 启动失败: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	fs := flag.NewFlagSet("netpol-reach", flag.ContinueOnError)
	cfg := config.FromEnv()
	config.BindFlags(fs, &cfg)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := cfg.Validate(); err != nil {
		return err
	}

	logger := diag.NewLogger(cfg.Debug)

	st, err := store.NewSQLiteStore(cfg.SQLiteDSN)
	if err != nil {
		return fmt.Errorf("打开 SQLite: %w", err)
	}
	defer st.Close()

	rec := reconcile.New(st)

	// 可选：启动时用本地数据包初始化。
	if cfg.SeedBundle != "" {
		if err := seedAndReconcile(context.Background(), st, rec, cfg, logger); err != nil {
			return err
		}
	} else {
		// 库中可能已有数据；尝试形成首个视图，失败只记录（可能是空库，属正常）。
		if _, err := rec.Reconcile(context.Background()); err != nil {
			logger.Reconcile(false, "initial reconcile (可能尚无数据)",
				"error", err.Error())
		}
	}

	srv := &httpapi.Server{Store: st, Rec: rec, Log: logger}
	httpServer := &http.Server{
		Addr:              cfg.Addr,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		logger.Slog().Info("http listening", "addr", cfg.Addr)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Slog().Error("http server error", "error", err.Error())
			os.Exit(1)
		}
	}()

	<-stop
	logger.Slog().Info("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return httpServer.Shutdown(ctx)
}

func seedAndReconcile(ctx context.Context, st store.Store, rec *reconcile.Reconciler, cfg config.Config, logger *diag.Logger) error {
	bundle, err := ingest.LoadBundleFile(cfg.SeedBundle)
	if err != nil {
		return fmt.Errorf("装载 seed 数据包: %w", err)
	}
	hasSnap, _ := st.HasSnapshot(ctx)
	hasPols, _ := st.HasPolicySet(ctx)
	overwrite := cfg.SeedOverwrite
	if !overwrite && (hasSnap || hasPols) {
		logger.Slog().Warn("库中已有数据且未指定 --seed-overwrite，跳过 seed 装载",
			"has_snapshot", hasSnap, "has_policy_set", hasPols)
	} else {
		if err := st.SaveSnapshot(ctx, bundle.Snapshot, overwrite); err != nil {
			return fmt.Errorf("写入 seed 快照: %w", err)
		}
		if err := st.SavePolicySet(ctx, bundle.PolicySet, overwrite); err != nil {
			return fmt.Errorf("写入 seed 策略集合: %w", err)
		}
	}

	v, err := rec.Reconcile(ctx)
	if err != nil {
		return fmt.Errorf("seed 后协调失败: %w", err)
	}
	logger.Reconcile(true, "seed reconcile ok",
		"label_version", v.Snapshot.LabelVersion,
		"policy_version", v.PolicySet.Version,
		"endpoints", len(v.Snapshot.Endpoints),
		"policies", len(v.PolicySet.Policies))
	return nil
}
