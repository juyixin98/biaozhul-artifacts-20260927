// Command topicrouter runs the hierarchical topic routing service.
//
// Usage:
//
//	topicrouter -config configs/config.example.json
//
// With no -config it starts an in-memory store on :8090, which is enough to
// walk through the examples in docs/API.md. TOPICROUTER_* environment
// variables override file configuration (see internal/config).
package main

import (
	"context"
	"errors"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"topicrouter/internal/config"
	"topicrouter/internal/diag"
	"topicrouter/internal/httpapi"
	"topicrouter/internal/router"
	"topicrouter/internal/store"
)

func main() {
	cfgPath := flag.String("config", "", "path to JSON configuration file")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		// Logger may not be configured yet; fail plainly.
		os.Stderr.WriteString("configuration error: " + err.Error() + "\n")
		os.Exit(2)
	}
	lg := diag.NewLogger(cfg.LogLevel)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	var st store.Store
	switch cfg.Store {
	case "memory":
		st = store.NewMemoryStore()
		lg.Record(ctx, diag.Event{Component: "main", Action: "startup",
			Outcome: diag.Accepted, Reason: "using in-memory store"})
	case "postgres":
		pg, err := store.OpenPG(ctx, cfg.Postgres.DSN,
			cfg.Postgres.MaxOpenConns, cfg.Postgres.MaxIdleConns)
		if err != nil {
			lg.Record(ctx, diag.Event{Component: "main", Action: "startup",
				Outcome: diag.Undecided, Category: "STORAGE_UNAVAILABLE",
				Reason: err.Error()})
			os.Exit(1)
		}
		defer pg.Close()
		st = pg
		lg.Record(ctx, diag.Event{Component: "main", Action: "startup",
			Outcome: diag.Accepted, Reason: "using postgres store"})
	}

	rt, err := router.New(ctx, st, lg)
	if err != nil {
		lg.Record(ctx, diag.Event{Component: "main", Action: "startup",
			Outcome: diag.Undecided, Category: "ROUTER_INIT", Reason: err.Error()})
		os.Exit(1)
	}

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           httpapi.NewServer(rt, lg),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		lg.Record(ctx, diag.Event{Component: "main", Action: "listen",
			Outcome: diag.Accepted, Reason: "HTTP server starting",
			KeyState: map[string]any{"addr": cfg.HTTPAddr}})
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			lg.Record(ctx, diag.Event{Component: "main", Action: "listen",
				Outcome: diag.Undecided, Category: "HTTP_SERVE", Reason: err.Error()})
			cancel()
		}
	}()

	<-ctx.Done()
	shutdownCtx, sCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer sCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		lg.Record(context.Background(), diag.Event{Component: "main", Action: "shutdown",
			Outcome: diag.Undecided, Category: "HTTP_SHUTDOWN", Reason: err.Error()})
	}
	lg.Record(context.Background(), diag.Event{Component: "main", Action: "shutdown",
		Outcome: diag.Accepted, Reason: "stopped"})
}
