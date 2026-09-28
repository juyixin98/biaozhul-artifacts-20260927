// Command reasm-server serves the offline IPv4 reassembly backend over
// HTTP on localhost. It accepts PCAP bytes or JSON fragments and persists
// run state in SQLite; it never initiates outbound network connections.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"ipreasm/internal/config"
	"ipreasm/internal/replay"
	"ipreasm/internal/store"
)

func main() {
	cfgPath := flag.String("config", "config/reasm.json", "config file")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	st, err := store.Open(cfg.DBPath)
	if err != nil {
		log.Fatalf("sqlite: %v", err)
	}
	defer st.Close()

	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           replay.NewServer(cfg, st).Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		log.Printf("reasm-server listening on http://%s (db=%s, timeout=%s)",
			cfg.Listen, cfg.DBPath, cfg.Timeout.Duration)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "shutdown: %v\n", err)
	}
}
