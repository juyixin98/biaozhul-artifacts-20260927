// Command controller runs the custom-resource controller: desired-state HTTP
// API + reconcile loop against the external resource service, with SQLite
// persistence.
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"resourcecontroller/internal/adapter"
	"resourcecontroller/internal/api"
	"resourcecontroller/internal/config"
	"resourcecontroller/internal/diag"
	"resourcecontroller/internal/reconcile"
	"resourcecontroller/internal/store"
)

func main() {
	cfg := config.FromEnv()
	log := diag.New(os.Stdout, "controller")

	// Best-effort: ensure the parent directory of a file: DSN exists.
	if dir := dsnDir(cfg.DatabaseDSN); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			log.Error(context.Background(), "cannot create database directory", "dir", dir, "error", err.Error())
			os.Exit(1)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DatabaseDSN)
	if err != nil {
		log.Error(ctx, "open store failed", "error", err.Error())
		os.Exit(1)
	}
	defer st.Close()

	ext := adapter.New(cfg.ExternalURL, log.With("component", "adapter"))
	rec := reconcile.New(st, ext, log.With("component", "reconcile"), reconcile.Config{
		Workers:        cfg.Workers,
		ResyncInterval: cfg.ResyncInterval,
		BackoffBase:    cfg.BackoffBase,
		BackoffMax:     cfg.BackoffMax,
	})
	go rec.Run(ctx)

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           (&api.Server{Store: st, Enqueuer: rec, Log: log.With("component", "api")}).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		log.Info(ctx, "controller listening",
			"addr", cfg.HTTPAddr, "externalURL", cfg.ExternalURL, "dsn", redactDSN(cfg.DatabaseDSN))
		serverErr <- srv.ListenAndServe()
	}()

	select {
	case <-ctx.Done():
	case err := <-serverErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error(ctx, "http server failed", "error", err.Error())
			os.Exit(1)
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error(shutdownCtx, "graceful shutdown failed", "error", err.Error())
	}
	rec.Stop()
	log.Info(context.Background(), "controller stopped")
}

// dsnDir extracts the directory from a "file:path?..." SQLite DSN.
func dsnDir(dsn string) string {
	rest := dsn
	if len(rest) > 5 && rest[:5] == "file:" {
		rest = rest[5:]
	}
	for i := 0; i < len(rest); i++ {
		if rest[i] == '?' {
			rest = rest[:i]
			break
		}
	}
	if rest == "" || rest == ":memory:" {
		return ""
	}
	dir := filepath.Dir(rest)
	if dir == "." {
		return ""
	}
	return dir
}

// redactDSN strips any query parameters that might carry credentials. The
// bundled DSN has none, but be defensive.
func redactDSN(dsn string) string {
	for i := 0; i < len(dsn); i++ {
		if dsn[i] == '?' {
			return dsn[:i] + "?<params>"
		}
	}
	return dsn
}
