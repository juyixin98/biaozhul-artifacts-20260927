// Command pvsim starts the path-vector convergence backend.
//
// Usage:
//
//	pvsim -addr 127.0.0.1:8080 -db ./pvsim.db
//
// All network traffic is loopback-only: the backend accepts synthetic
// scenario documents and never dials external BGP peers.
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

	"pvsim/api"
	"pvsim/replay"
	"pvsim/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address (loopback by default)")
	dbPath := flag.String("db", "pvsim.db", "SQLite database path (use :memory: for ephemeral)")
	flag.Parse()

	st, err := store.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open store: %v\n", err)
		os.Exit(1)
	}
	defer st.Close()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(replay.NewService(st), st).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("pvsim listening on %s (db=%s)", *addr, *dbPath)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
