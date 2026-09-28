// Command cidrcovd runs the minimal-CIDR-cover HTTP service.
//
// Usage:
//
//	cidrcovd [-config configs/service.json] [-listen 127.0.0.1:8080]
//	         [-db cidrcov.db] [-log-level info]
//
// Flags beat config-file values; CIDRCOV_* environment variables (documented
// in README) beat both at the config layer but explicit flags win overall.
package main

import (
	"context"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"cidrcov/internal/api"
	"cidrcov/internal/config"
	"cidrcov/internal/engine"
	"cidrcov/internal/store"
)

func main() {
	cfgPath := flag.String("config", "", "path to JSON config file")
	listen := flag.String("listen", "", "HTTP listen address (overrides config)")
	dbPath := flag.String("db", "", "SQLite path (overrides config; :memory: allowed)")
	levelName := flag.String("log-level", "info", "log level: debug|info|warn|error")
	flag.Parse()

	level := slog.LevelInfo
	switch *levelName {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		logger.Error("configuration error", "reason", err.Error())
		os.Exit(2)
	}
	if *listen != "" {
		cfg.Listen = *listen
	}
	if *dbPath != "" {
		cfg.DBPath = *dbPath
	}
	if err := cfg.Validate(); err != nil {
		logger.Error("configuration error", "reason", err.Error())
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DBPath)
	if err != nil {
		logger.Error("storage open failed", "reason", err.Error())
		os.Exit(1)
	}
	defer st.Close()

	srv := &api.Server{
		EngineOpts: engine.Options{MaxEntriesPerList: cfg.MaxEntriesPerList},
		Store:      st,
		Logger:     logger,
	}
	httpSrv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Info("cidrcovd starting",
			"listen", cfg.Listen, "db", cfg.DBPath,
			"service_version", engine.ServiceVersion,
			"algorithm_version", engine.AlgorithmVersion)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("http server failed", "reason", err.Error())
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	logger.Info("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(),
		time.Duration(cfg.ShutdownTimeoutMS)*time.Millisecond)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		logger.Error("graceful shutdown failed", "reason", err.Error())
		_ = httpSrv.Close()
	}
	logger.Info("cidrcovd stopped")
}
