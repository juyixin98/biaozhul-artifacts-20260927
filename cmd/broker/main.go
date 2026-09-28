// Command broker runs the local work-message broker HTTP server.
//
// Storage:
//
//	BROKER_DRIVER=mem       in-memory store (default; state dies on exit)
//	BROKER_DRIVER=postgres  PostgreSQL store (BROKER_POSTGRES_DSN)
//
// Example:
//
//	go run ./cmd/broker
//	BROKER_DRIVER=postgres BROKER_POSTGRES_DSN='host=/var/run/postgresql user=admin dbname=brokertest sslmode=disable' \
//	  go run ./cmd/broker
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"localbroker/internal/broker"
	"localbroker/internal/config"
	"localbroker/internal/httpapi"
	"localbroker/internal/protocol"
	"localbroker/internal/store"
	"localbroker/internal/storemem"
	"localbroker/internal/storepg"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	cfg, err := config.FromEnv(config.Default())
	if err != nil {
		fatalf("config: %v", err)
	}
	if cfg.RunID == "" {
		cfg.RunID = fmt.Sprintf("run-pid%d-%d", os.Getpid(), time.Now().UnixNano())
	}

	ctx := context.Background()
	st, closeStore, err := openStore(ctx, cfg)
	if err != nil {
		fatalf("store: %v", err)
	}
	defer closeStore()

	b := broker.New(st, broker.WithRunID(cfg.RunID), broker.WithSweeper(cfg.SweepInterval))
	defer b.Close()

	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           httpapi.NewServer(b).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("broker %s starting driver=%s addr=%s run_id=%s sweep=%s",
		protocol.Version, cfg.Driver, cfg.HTTPAddr, cfg.RunID, cfg.SweepInterval)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fatalf("http: %v", err)
	}
}

func openStore(ctx context.Context, cfg config.Config) (store.Store, func(), error) {
	switch cfg.Driver {
	case "mem":
		st := storemem.New()
		return st, func() { _ = st.Close() }, nil
	case "postgres":
		st, err := storepg.Open(ctx, cfg.PostgresDSN)
		if err != nil {
			return nil, func() {}, err
		}
		return st, func() { _ = st.Close() }, nil
	default:
		return nil, func() {}, fmt.Errorf("unknown driver %q", cfg.Driver)
	}
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}
