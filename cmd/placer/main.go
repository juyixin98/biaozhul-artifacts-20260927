// Command placer runs the node-placement backend: an HTTP adapter, a
// SQLite inventory and an optional periodic reconciliation loop.
package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"placer/internal/config"
	"placer/internal/logx"
	"placer/internal/reconcile"
	"placer/internal/server"
	"placer/internal/store"
	"placer/internal/version"
)

func main() {
	cfgPath := flag.String("config", "configs/placer.json", "path to JSON config (missing file => defaults)")
	fixture := flag.String("fixture", "", "load synthetic fixture JSON into the DB at startup, replacing current data")
	addrFlag := flag.String("addr", "", "override http_addr")
	dbFlag := flag.String("db", "", "override database path")
	oneShot := flag.Bool("once", false, "run a single reconcile pass after optional fixture load and exit")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		// Logger may not exist yet; stderr exit is the honest failure.
		os.Stderr.WriteString("config error: " + err.Error() + "\n")
		os.Exit(2)
	}
	if *addrFlag != "" {
		cfg.HTTPAddr = *addrFlag
	}
	if *dbFlag != "" {
		cfg.Database = *dbFlag
	}
	level, _ := logx.ParseLevel(cfg.LogLevel)
	log := logx.New(os.Stderr, level).With("main", "")
	log.Info("starting", map[string]any{"config": *cfgPath, "version": version.String()})

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	st, err := store.Open(ctx, cfg.Database)
	if err != nil {
		log.Error("store_open_failed", err, nil)
		os.Exit(1)
	}
	defer st.Close()

	if *fixture != "" {
		if err := loadFixture(ctx, st, *fixture, log); err != nil {
			log.Error("fixture_load_failed", err, map[string]any{"fixture": *fixture})
			os.Exit(1)
		}
	}

	loop := reconcile.New(st, cfg, log)

	if *oneShot {
		sum, err := loop.RunOnce(ctx, logx.NewRunID())
		if err != nil {
			os.Exit(1)
		}
		log.Info("oneshot_done", map[string]any{"status": string(sum.Status), "pending": sum.Pending})
		return
	}

	if err := loop.Start(ctx, cfg.ReconcileIntervalSec); err != nil {
		log.Error("loop_start_failed", err, nil)
		os.Exit(1)
	}
	defer loop.Stop()

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           server.New(st, loop, log).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		log.Info("http_listening", map[string]any{"addr": cfg.HTTPAddr})
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("http_server_failed", err, nil)
			cancel()
		}
	}()

	<-ctx.Done()
	log.Info("shutting_down", nil)
	shutdownCtx, c := context.WithTimeout(context.Background(), 5*time.Second)
	defer c()
	_ = srv.Shutdown(shutdownCtx)
}
