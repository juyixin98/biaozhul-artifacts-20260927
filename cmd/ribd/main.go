// Command ribd runs the routing information base daemon: it loads
// configuration, opens state storage, replays persisted batches (or installs
// the bootstrap routes from config on first run) and serves the HTTP API.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/opp221/ribd/internal/config"
	"github.com/opp221/ribd/internal/diag"
	"github.com/opp221/ribd/internal/replay"
	"github.com/opp221/ribd/internal/rib"
	"github.com/opp221/ribd/internal/server"
	"github.com/opp221/ribd/internal/store"
)

func main() {
	cfgPath := flag.String("config", "testdata/config/example.json", "path to config file")
	bootstrapOnly := flag.Bool("bootstrap-only", false, "install config routes into a fresh store and exit")
	flag.Parse()

	if err := run(*cfgPath, *bootstrapOnly); err != nil {
		fmt.Fprintf(os.Stderr, "ribd: %v\n", err)
		os.Exit(1)
	}
}

func run(cfgPath string, bootstrapOnly bool) error {
	cfg, err := config.LoadFile(cfgPath)
	if err != nil {
		return err
	}
	logger := diag.NewLogger(os.Stderr)
	defer logger.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	var st *store.Store
	var persister rib.Persister
	if cfg.Storage.Driver == "sqlite" {
		st, err = store.Open(ctx, cfg.Storage.DSN)
		if err != nil {
			return err
		}
		defer st.Close()
		persister = st
	}

	table := rib.NewTable(cfg.Resolution.MaxDepth, persister)

	// Cold-start recovery: if a store exists with events, replay it and adopt
	// the reconstructed state directly (the store is authoritative).
	if st != nil {
		head, err := st.HeadVersion(ctx)
		if err != nil {
			return err
		}
		switch {
		case head > 0:
			res, err := replay.FromStore(ctx, st, cfg.Resolution.MaxDepth, head)
			if err != nil {
				return fmt.Errorf("cold-start replay: %w", err)
			}
			if err := table.Adopt(res.Snap); err != nil {
				return err
			}
			logger.Log(diag.Record{Event: "startup.replayed", TableVer: head,
				Detail: fmt.Sprintf("reconstructed %d routes from %d batches", len(res.Snap.Routes()), res.Applied)})
		case len(cfg.Routes) > 0:
			// Fresh database: install config routes as version 1 in a single
			// batch so bootstrap routes appear atomically.
			changes := make([]rib.Change, 0, len(cfg.Routes))
			for _, r := range cfg.Routes {
				changes = append(changes, rib.Change{Kind: rib.Upsert, Route: r})
			}
			snap, err := table.Apply(changes)
			if err != nil {
				return fmt.Errorf("bootstrap: %w", err)
			}
			logger.Log(diag.Record{Event: "startup.bootstrap", TableVer: snap.VersionNum(),
				Detail: fmt.Sprintf("installed %d bootstrap routes", len(changes))})
		}
	} else if len(cfg.Routes) > 0 {
		changes := make([]rib.Change, 0, len(cfg.Routes))
		for _, r := range cfg.Routes {
			changes = append(changes, rib.Change{Kind: rib.Upsert, Route: r})
		}
		if _, err := table.Apply(changes); err != nil {
			return fmt.Errorf("bootstrap: %w", err)
		}
	}

	if bootstrapOnly {
		return nil
	}

	srv := &http.Server{
		Addr:         cfg.Server.Listen,
		Handler:      server.New(table, st, logger).Handler(),
		ReadTimeout:  time.Duration(cfg.Server.ReadTimeoutMS) * time.Millisecond,
		WriteTimeout: time.Duration(cfg.Server.WriteTimeoutMS) * time.Millisecond,
	}

	go func() {
		logger.Log(diag.Record{Event: "startup.listen", Detail: cfg.Server.Listen,
			State: map[string]any{"storage": cfg.Storage.Driver, "max_depth": cfg.Resolution.MaxDepth}})
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Fprintf(os.Stderr, "ribd: serve: %v\n", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return srv.Shutdown(shutdownCtx)
}

// ensure encoding/json is retained for future envelope extensions.
var _ = json.Marshal
