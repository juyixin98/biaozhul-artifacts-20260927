// Command admissiond starts the local admission HTTP service: it loads the
// startup config and local defaults catalog, opens SQLite, wires the ordered
// mutator/validator chains, runs the background reconciler (crash-reclaim),
// and serves the stdlib HTTP API.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"admission/internal/adapter"
	"admission/internal/app"
	"admission/internal/config"
)

func main() {
	cfgPath := flag.String("config", "configs/admissiond.json", "path to startup config")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	catalog, err := config.LoadDefaultsCatalog(cfg.DefaultsFile)
	if err != nil {
		log.Fatalf("load defaults catalog: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	system, err := app.Build(ctx, cfg, catalog)
	if err != nil {
		log.Fatalf("build system: %v", err)
	}
	defer system.Close()

	// Background reconciliation reclaims stale processing leases (crash
	// recovery) and drains rows inserted while no worker was active.
	go system.Coordinator.RunReconcile(ctx, time.Duration(cfg.ReconcileMS)*time.Millisecond)

	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           adapter.NewHandler(system.Coordinator, system.Store),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		log.Printf("admissiond listening on %s (db=%s, runlog=%s)", cfg.Listen, cfg.DatabasePath, cfg.LogDir)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
	os.Exit(0)
}
