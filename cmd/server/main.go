// Command fwrule-server runs the local firewall-rule analysis & replay
// service. It is fully local: SQLite on disk (or in-memory), no external
// network dependencies.
//
// Usage:
//
//	fwrule-server -addr :8080 -db ./data/fwrule.db -policy ./configs/policy.json
//
// If -policy is provided it is loaded as version 1 on startup (only when the
// database is empty). -db :memory: keeps everything in process.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"fwrule/internal/analyzer"
	"fwrule/internal/httpapi"
	"fwrule/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dbPath := flag.String("db", "./data/fwrule.db", "SQLite path (use :memory: for ephemeral)")
	policyPath := flag.String("policy", "", "optional policy JSON to load as version 1 when DB is empty")
	flag.Parse()

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	if *dbPath != ":memory:" {
		if err := os.MkdirAll(dbDir(*dbPath), 0o755); err != nil {
			log.Fatalf("create db dir: %v", err)
		}
	}
	st, err := store.Open(ctx, *dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	if *policyPath != "" {
		if err := bootstrapPolicy(ctx, st, *policyPath); err != nil {
			log.Fatalf("bootstrap policy: %v", err)
		}
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.New(st).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		log.Printf("fwrule-server listening on %s (db=%s)", *addr, *dbPath)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()
	<-ctx.Done()
	log.Printf("shutting down")
	shCtx, shCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shCancel()
	_ = srv.Shutdown(shCtx)
}

func bootstrapPolicy(ctx context.Context, st *store.Store, path string) error {
	if _, err := st.LatestVersion(ctx); err == nil {
		log.Printf("store already contains a policy; skipping bootstrap")
		return nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	pv, pol, err := st.SavePolicy(ctx, path, raw)
	if err != nil {
		return err
	}
	rep := analyzer.Analyze(pol, pv.Version)
	if err := st.SaveReport(ctx, pv.Version, rep); err != nil {
		return err
	}
	log.Printf("bootstrapped policy %q as version %d (%d rules)",
		pv.Name, pv.Version, len(pol.Rules))
	return nil
}

func dbDir(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' {
			if i == 0 {
				return "/"
			}
			return p[:i]
		}
	}
	return "."
}
