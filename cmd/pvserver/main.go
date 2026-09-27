// Command pvserver serves the synthetic path-vector replay API on
// localhost. It does not dial any real BGP peer: all state changes come
// from JSON replay requests or local fixture files.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"pathvector/internal/api"
	"pathvector/internal/replay"
	"pathvector/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address (localhost only by default)")
	dbPath := flag.String("db", "data/pvserver.db", "SQLite database path (use :memory: for ephemeral)")
	fixtureDir := flag.String("fixtures", "testdata/fixtures", "directory holding named fixture JSON files")
	logPath := flag.String("log", "logs/pvserver.log", "decision log file (empty = stderr only)")
	flag.Parse()

	if *dbPath != ":memory:" {
		if err := os.MkdirAll(filepath.Dir(*dbPath), 0o755); err != nil {
			log.Fatalf("create db dir: %v", err)
		}
	}
	var logWriters = []io.Writer{os.Stderr}
	if *logPath != "" {
		if err := os.MkdirAll(filepath.Dir(*logPath), 0o755); err != nil {
			log.Fatalf("create log dir: %v", err)
		}
		f, err := os.OpenFile(*logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			log.Fatalf("open log: %v", err)
		}
		defer f.Close()
		logWriters = append(logWriters, f)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	st, err := store.Open(ctx, *dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	runner := replay.NewRunner(st, logWriters...)
	srv := &api.Server{Runner: runner, Store: st, FixtureDir: *fixtureDir, MaxBodySize: 4 << 20}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.NewRouter(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		fmt.Fprintf(os.Stderr, "pvserver listening on %s (db=%s fixtures=%s)\n", *addr, *dbPath, *fixtureDir)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()
	<-ctx.Done()
	fmt.Fprintln(os.Stderr, "\nshutting down")
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutCancel()
	_ = httpSrv.Shutdown(shutCtx)
}
