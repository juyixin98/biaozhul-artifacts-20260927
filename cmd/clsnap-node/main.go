// Command clsnap-node runs one Chandy-Lamport snapshot process.
//
// Usage:
//
//	clsnap-node -config configs/n1.json [-store memory|postgres] [-run run-id]
//
// The process serves the documented HTTP API and participates in snapshots
// started by any node.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"clsnap/internal/node"
	"clsnap/internal/store"
)

type storeCfg struct {
	Driver string `json:"driver"`
	DSN    string `json:"dsn"`
	Schema string `json:"schema"`
}

type config struct {
	RunID           string             `json:"run_id"`
	Listen          string             `json:"listen"`
	NodeID          string             `json:"node_id"`
	InitialBalances map[string]int64    `json:"initial_balances"`
	Store           storeCfg           `json:"store"`
	Peers           []node.Peer        `json:"peers"`
	OutboxCap       int                `json:"outbox_cap"`
	PumpIntervalMs  int                `json:"pump_interval_ms"`
}

func main() {
	cfgPath := flag.String("config", "configs/n1.json", "path to node config")
	storeDriver := flag.String("store", "", "override store driver: memory|postgres")
	runID := flag.String("run", "", "override run id")
	flag.Parse()

	raw, err := os.ReadFile(*cfgPath)
	if err != nil {
		log.Fatalf("read config: %v", err)
	}
	var cfg config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		log.Fatalf("parse config: %v", err)
	}
	if *storeDriver != "" {
		cfg.Store.Driver = *storeDriver
	}
	if *runID != "" {
		cfg.RunID = *runID
	}
	if cfg.RunID == "" {
		cfg.RunID = "run-" + time.Now().UTC().Format("20060102T150405Z")
	}

	ctx, cancel := signalContext()
	defer cancel()

	var st store.Store
	switch cfg.Store.Driver {
	case "postgres", "pg":
		st, err = store.OpenPg(ctx, store.PgConfig{
			DSN: cfg.Store.DSN, Schema: cfg.Store.Schema,
			NodeID: cfg.NodeID, RunID: cfg.RunID, OutboxCap: cfg.OutboxCap,
		})
		if err != nil {
			log.Fatalf("open postgres: %v", err)
		}
	default:
		peerIDs := make([]string, len(cfg.Peers))
		for i, p := range cfg.Peers {
			peerIDs[i] = p.ID
		}
		st = store.NewMemoryStore(cfg.NodeID, cfg.RunID, peerIDs, cfg.OutboxCap)
	}

	n, err := node.New(ctx, node.Config{
		NodeID:          cfg.NodeID,
		RunID:           cfg.RunID,
		InitialBalances: cfg.InitialBalances,
		Peers:           cfg.Peers,
		OutboxCap:       cfg.OutboxCap,
		Store:           st,
		Transport:       node.NewHTTPTransport(),
		PumpInterval:    time.Duration(cfg.PumpIntervalMs) * time.Millisecond,
	})
	if err != nil {
		log.Fatalf("build node: %v", err)
	}
	n.Run(ctx)

	srv := &http.Server{Addr: cfg.Listen, Handler: n.Handler()}
	go func() {
		log.Printf("clsnap node %s listening on %s store=%s run=%s",
			cfg.NodeID, cfg.Listen, cfg.Store.Driver, cfg.RunID)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	shCtx, shCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer shCancel()
	_ = srv.Shutdown(shCtx)
	_ = n.Shutdown(shCtx)
	log.Printf("node %s stopped", cfg.NodeID)
}

func signalContext() (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-ch
		cancel()
	}()
	return ctx, cancel
}
