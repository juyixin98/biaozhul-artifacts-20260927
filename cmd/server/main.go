// Command server runs the local infrastructure planner with an in-process
// simulated provider and a SQLite journal. Everything is local: no network
// calls other than the HTTP listener.
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

	"infraplanner/internal/httpapi"
	"infraplanner/internal/journal"
	"infraplanner/internal/logging"
	"infraplanner/internal/provider"
	"infraplanner/internal/reconciler"
)

func main() {
	addr := flag.String("addr", envOr("ADDR", "127.0.0.1:8080"), "listen address")
	dbPath := flag.String("db", envOr("DB_PATH", "./data/planner.db"), "SQLite journal path")
	logDir := flag.String("logdir", envOr("LOG_DIR", "./data/logs"), "per-run JSONL log directory")
	echo := flag.Bool("echo", envOr("ECHO", "true") == "true", "echo run logs to stdout")
	flag.Parse()

	if err := os.MkdirAll("./data", 0o755); err != nil {
		log.Fatalf("data dir: %v", err)
	}
	store, err := journal.Open(*dbPath)
	if err != nil {
		log.Fatalf("open journal %s: %v", *dbPath, err)
	}
	defer store.Close()

	sim := provider.NewSim()
	rec := reconciler.New(store, sim, 3)
	rec.SetLogger(func(runID string) (reconciler.Logger, error) {
		return logging.New(runID, *logDir, *echo)
	})

	srv := httpapi.New(rec, sim)

	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           srv.Mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		fmt.Printf("infrastructure planner listening on http://%s (db=%s logs=%s)\n",
			*addr, *dbPath, *logDir)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(ctx)
	fmt.Println("server stopped")
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
