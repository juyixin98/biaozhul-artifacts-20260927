// Command rollctl runs the local rolling-release controller with its HTTP API
// and a simulated process manager. All state lives in one local SQLite file;
// no external services are required.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"rollctl/internal/adapter"
	"rollctl/internal/api"
	"rollctl/internal/config"
	"rollctl/internal/controller"
	"rollctl/internal/store"
)

func main() {
	cfg, err := config.Parse(os.Args[1:])
	if err != nil {
		os.Exit(2)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if dir := filepath.Dir(cfg.DBPath); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			logger.Error("create db dir", "err", err)
			os.Exit(1)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DBPath)
	if err != nil {
		logger.Error("open store", "path", cfg.DBPath, "err", err)
		os.Exit(1)
	}
	defer st.Close()

	sim, err := adapter.NewSimulator(ctx, st, cfg.SimCapacity)
	if err != nil {
		logger.Error("init simulator", "err", err)
		os.Exit(1)
	}

	ctl, err := controller.New(ctx, st, sim, controller.Options{Logger: logger})
	if err != nil {
		logger.Error("init controller", "err", err)
		os.Exit(1)
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           api.NewServer(ctl, sim, logger).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	// Auto reconcile loop (disabled in manual tick mode).
	if cfg.TickPeriod > 0 {
		go ctl.Run(ctx, cfg.TickPeriod)
	}

	go func() {
		logger.Info("rollctl listening", "addr", cfg.HTTPAddr, "db", cfg.DBPath,
			"tick_period", cfg.TickPeriod.String(), "sim_capacity", cfg.SimCapacity)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http server", "err", err)
			stop()
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}
