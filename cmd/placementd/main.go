// Command placementd starts the node-placement backend: an in-process SQLite
// database, a standard-library HTTP server and an optional periodic
// reconciliation loop.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"opp284/placement/internal/api"
	"opp284/placement/internal/config"
	"opp284/placement/internal/engine"
	"opp284/placement/internal/logging"
	"opp284/placement/internal/model"
	"opp284/placement/internal/reconcile"
	"opp284/placement/internal/scheduler"
	"opp284/placement/internal/store"
	"opp284/placement/internal/version"
)

func main() {
	configPath := flag.String("config", "", "path to JSON config (defaults built-in)")
	dbDSN := flag.String("dsn", "", "override storage.dsn (e.g. file:placement.db)")
	addr := flag.String("addr", "", "override http.addr")
	reset := flag.Bool("reset", false, "reset database schema on startup")
	seed := flag.Bool("seed", false, "seed the demo cluster 'demo' with synthetic nodes")
	clusterID := flag.String("cluster", "demo", "default cluster id for the reconciler")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		fatal("config: " + err.Error())
	}
	if *dbDSN != "" {
		cfg.Storage.DSN = *dbDSN
	}
	if *addr != "" {
		cfg.HTTP.Addr = *addr
	}
	if *reset {
		cfg.Storage.Reset = true
	}

	runID := "run-" + randHex(6)
	level, err := logging.ParseLevel(cfg.Logging.Level)
	if err != nil {
		fatal(err.Error())
	}
	log := logging.New(os.Stderr, level, cfg.Logging.Human, runID)
	log.OK("starting placementd", map[string]any{
		"version": version.String(), "addr": cfg.HTTP.Addr, "dsn": cfg.Storage.DSN,
	})

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.Storage.DSN, cfg.Storage.Reset)
	if err != nil {
		fatal("store: " + err.Error())
	}
	defer st.Close()

	if *seed {
		if err := seedDemoCluster(ctx, st, *clusterID); err != nil {
			fatal("seed: " + err.Error())
		}
		log.OK("demo cluster seeded", map[string]any{"cluster_id": *clusterID})
	}

	limits := scheduler.SearchLimits{
		MaxInstances:   cfg.Scheduler.ExhaustiveMaxInstances,
		MaxCandidates:  cfg.Scheduler.ExhaustiveMaxCandidates,
		MaxLeafVisits:  cfg.Scheduler.MaxLeafVisits,
		IncludeEmptyDZ: cfg.Scheduler.Skew.IncludeDeclaredEmpty,
	}
	eng := engine.New(st, limits, log)

	provider := reconcile.NewStaticProvider(nil)
	rec := reconcile.New(eng, st, provider, cfg.Reconcile.AllowRecreate,
		cfg.Reconcile.MaxPlansPerTick, log)

	interval, err := time.ParseDuration(cfg.Reconcile.Interval)
	if err != nil {
		fatal("reconcile.interval: " + err.Error())
	}
	if interval > 0 {
		go rec.RunPeriodic(ctx, *clusterID, interval)
	}

	srv := &http.Server{
		Addr:              cfg.HTTP.Addr,
		Handler:           api.NewServer(eng, st, rec, log, *clusterID).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		log.OK("http listening", map[string]any{"addr": cfg.HTTP.Addr})
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fail("http server", map[string]any{"err": err.Error()})
			stop()
		}
	}()

	<-ctx.Done()
	log.Info("shutdown signal received", nil)
	if err := api.Shutdown(srv, cfg.HTTP.ShutdownTimeout); err != nil {
		log.Fail("graceful shutdown", map[string]any{"err": err.Error()})
		os.Exit(1)
	}
	log.OK("shutdown complete", nil)
}

func seedDemoCluster(ctx context.Context, st *store.Store, cid string) error {
	cap := model.Resources{"cpu": 8, "mem": 16}
	nodes := []model.Node{
		{ID: "n-1", Zone: "z-a", Capacity: cap.Clone(), Labels: model.Labels{"role": "web"}, Eligible: true},
		{ID: "n-2", Zone: "z-a", Capacity: cap.Clone(), Labels: model.Labels{"role": "web"}, Eligible: true},
		{ID: "n-3", Zone: "z-b", Capacity: cap.Clone(), Labels: model.Labels{"role": "web"}, Eligible: true},
		{ID: "n-4", Zone: "z-b", Capacity: cap.Clone(), Labels: model.Labels{"role": "web"}, Eligible: true},
		{ID: "n-5", Zone: "z-c", Capacity: cap.Clone(), Labels: model.Labels{"role": "db"}, Eligible: true},
		{ID: "n-6", Zone: "z-c", Capacity: cap.Clone(), Labels: model.Labels{"role": "web"}, Eligible: false},
	}
	return st.UpsertCluster(ctx, cid, []string{"z-a", "z-b", "z-c"}, nodes, nil, nil)
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "000000000000"
	}
	return hex.EncodeToString(b)
}

func fatal(msg string) {
	os.Stderr.WriteString("placementd: " + msg + "\n")
	os.Exit(2)
}
