// Command replicactl runs the local replica-count controller.
//
// It serves the HTTP API and runs a background reconciliation loop. All data
// is local synthetic fixture data stored in SQLite; there are no external
// services.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"replicactl/internal/app"
	"replicactl/internal/config"
)

func main() {
	var (
		dsn        = flag.String("db", envOr("REPLICACTL_DB", "file:replicactl.db"), "SQLite DSN")
		addr       = flag.String("addr", envOr("REPLICACTL_ADDR", ""), "listen address (overrides config)")
		configPath = flag.String("config", envOr("REPLICACTL_CONFIG", ""), "optional JSON config file")
		noAuto     = flag.Bool("no-autotick", os.Getenv("REPLICACTL_NO_AUTOTICK") == "1", "disable background loop (reconcile only via HTTP)")
	)
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	cfg := config.Default()
	if *configPath != "" {
		b, err := os.ReadFile(*configPath)
		if err != nil {
			logger.Error("read_config", "error", err)
			os.Exit(2)
		}
		if err := json.Unmarshal(b, &cfg); err != nil {
			logger.Error("parse_config", "error", err)
			os.Exit(2)
		}
	}
	if *addr != "" {
		cfg.ListenAddr = *addr
	}

	opt := app.Options{DSN: *dsn, Config: &cfg, Logger: logger}
	if !*noAuto {
		opt.AutoReconcile = cfg.TickInterval.Duration
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	a, err := app.New(context.Background(), opt)
	if err != nil {
		logger.Error("startup", "error", err)
		os.Exit(1)
	}
	defer func() { _ = a.Close() }()

	go func() {
		logger.Info("http_listening", "addr", a.Server.Addr, "tick_interval", cfg.TickInterval.String())
		if err := a.Server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http_serve", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	logger.Info("shutdown")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = a.Server.Shutdown(shutdownCtx)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
