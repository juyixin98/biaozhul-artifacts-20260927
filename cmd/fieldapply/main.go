// Command fieldapply runs the declarative field-level merge backend.
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

	"fieldapply/internal/coord"
	"fieldapply/internal/diag"
	"fieldapply/internal/httpapi"
	"fieldapply/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("ADDR", "127.0.0.1:8080"), "listen address")
	dbPath := flag.String("db", envOr("DB", "fieldapply.db"), "SQLite database path (use ':memory:' for ephemeral)")
	journal := flag.String("journal", envOr("JOURNAL", "fieldapply.journal.jsonl"), "diagnostics journal path ('-' for stdout, '' to disable)")
	flag.Parse()

	logger, closeLog, err := openJournal(*journal)
	if err != nil {
		log.Fatalf("journal: %v", err)
	}
	defer closeLog()

	st, err := openStore(*dbPath)
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	defer st.Close()

	co := coord.New(st, logger)
	defer co.Close()

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.New(st, co).Mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("fieldapply listening on %s (db=%s)", *addr, *dbPath)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Print("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}

func openStore(path string) (store.Store, error) {
	if path == ":memory:" {
		return store.NewMemory(), nil
	}
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)", path)
	return store.OpenSQLite(dsn)
}

func openJournal(path string) (*diag.Logger, func() error, error) {
	switch path {
	case "":
		return diag.NewLogger(nil), func() error { return nil }, nil
	case "-":
		return diag.NewLogger(os.Stdout), func() error { return nil }, nil
	default:
		l, err := diag.OpenFile(path)
		if err != nil {
			return nil, nil, err
		}
		return l, l.Close, nil
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
