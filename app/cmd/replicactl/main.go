// Command replicactl runs the local replica controller HTTP service. All
// state lives in one local SQLite file and the only workload is the in-process
// synthetic fixture; pass -config for a reproducible configuration.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"replicactl/app/config"
	"replicactl/app/server"
	"replicactl/app/store"
	"replicactl/core/controller"
)

func main() {
	configPath := flag.String("config", "config/controller.json", "path to JSON config")
	addr := flag.String("addr", "", "override HTTP listen address")
	dsn := flag.String("db", "", "override SQLite DSN")
	flag.Parse()

	logger := log.New(os.Stdout, "replicactl ", log.LstdFlags|log.Lmicroseconds)

	cfg, rt, err := config.Load(*configPath)
	if err != nil {
		logger.Fatalf("step=config_failed detail=%q", err)
	}
	if *addr != "" {
		rt.HTTPAddr = *addr
	}
	if *dsn != "" {
		rt.DatabaseDSN = *dsn
	}
	logger.Printf("step=config_loaded target_per_instance=%v up_factor=%v up_floor=%v down_window=%ds stale_skew=%ds tolerance=%.2f min_fresh=%.2f range=[%d,%d] bootstrap=%d",
		cfg.TargetLoadPerInstance, cfg.MaxScaleUpFactor, cfg.MaxScaleUpFloor,
		cfg.ScaleDownStableWindow, cfg.StaleSkew, cfg.Tolerance, cfg.MinFreshFraction,
		cfg.MinReplicas, cfg.MaxReplicas, cfg.BootstrapReplicas)

	db, err := store.Open(rt.DatabaseDSN)
	if err != nil {
		logger.Fatalf("step=store_open_failed detail=%q", err)
	}
	defer db.Close()

	decisions := store.NewDecisionLog(db)
	history := store.NewRawPointLog(db)
	fixture, err := store.NewDurableFixture(db, cfg)
	if err != nil {
		logger.Fatalf("step=fixture_failed detail=%q", err)
	}
	faultyDecisions := &server.FaultyDecisionStore{Inner: decisions}
	faultyHistory := &server.FaultyHistory{Inner: history}
	ctl, err := controller.New(cfg, fixture, fixture, faultyDecisions, faultyHistory)
	if err != nil {
		logger.Fatalf("step=controller_failed detail=%q", err)
	}

	srv := server.New(ctl, fixture, faultyDecisions, faultyHistory, logger)
	srv.RawDecisions = decisions
	srv.FaultyDecisions = faultyDecisions
	srv.FaultyHistory = faultyHistory
	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe(rt.HTTPAddr) }()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		logger.Fatalf("step=http_stopped detail=%q", err)
	case sig := <-stop:
		logger.Printf("step=shutdown_start signal=%s", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			logger.Printf("step=shutdown_error detail=%q", err)
		}
		logger.Printf("step=shutdown_complete")
	}
}
