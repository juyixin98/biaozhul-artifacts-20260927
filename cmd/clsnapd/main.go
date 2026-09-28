// Command clsnapd runs one Chandy-Lamport snapshot node process.
//
// Usage:
//
//	clsnapd -config configs/n1.json
//
// The node serves the HTTP API described in docs/API.md, recovers (aborts)
// unfinished snapshot rounds from earlier boots, and periodically flushes its
// durable outbox. Three such processes on different ports form the teaching
// cluster. Each node config carries the full account table; the node keeps
// only the accounts whose owner is itself, and uses the rest for routing.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"clsnap/internal/config"
	"clsnap/internal/protocol"
	"clsnap/internal/server"
	"clsnap/internal/store"
)

func main() {
	cfgPath := flag.String("config", "configs/n1.json", "path to node config JSON")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "config error:", err)
		os.Exit(2)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// Full table for routing; local slice for the ledger seed.
	fullTable := map[protocol.NodeID][]protocol.Account{}
	var local []protocol.Account
	for _, a := range cfg.Accounts {
		fullTable[a.Owner] = append(fullTable[a.Owner], a)
		if a.Owner == cfg.ID {
			local = append(local, a)
		}
	}
	cfg.Accounts = local

	var st store.Store
	switch cfg.StoreDriver {
	case "postgres":
		st, err = store.OpenPostgres(ctx, cfg.PostgresDSN)
		if err != nil {
			fmt.Fprintln(os.Stderr, "postgres:", err)
			os.Exit(1)
		}
	case "memory":
		st = store.NewMemory()
	default:
		fmt.Fprintln(os.Stderr, "unknown store driver", cfg.StoreDriver)
		os.Exit(2)
	}
	defer st.Close()

	node, err := server.NewNode(ctx, server.Deps{Cfg: cfg, St: st})
	if err != nil {
		fmt.Fprintln(os.Stderr, "node init:", err)
		os.Exit(1)
	}
	node.MergeRouterAccounts(fullTable)
	node.StartPump(ctx)

	if _, err := node.ListenAndServe(ctx); err != nil {
		log.Fatal(err)
	}
	log.Printf("node %s listening on %s (epoch=%d, driver=%s)",
		cfg.ID, cfg.Listen, node.Coord.Epoch(), cfg.StoreDriver)

	<-ctx.Done()
	node.StopPump()
	log.Printf("node %s stopped", cfg.ID)
}
