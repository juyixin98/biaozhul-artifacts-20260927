// Command fieldmerged runs the field-level merge backend:
// SQLite storage, HTTP API and the reconciliation loop.
package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"fieldmerge/internal/adapter"
	"fieldmerge/internal/log"
	"fieldmerge/internal/reconcile"
	"fieldmerge/internal/server"
	"fieldmerge/internal/store"
)

func main() {
	var (
		addr    = flag.String("addr", ":8080", "HTTP listen address")
		dataDir = flag.String("data", "data", "data directory (sqlite db + adapter output + logs)")
		workers = flag.Int("workers", 2, "reconcile workers")
		queue   = flag.Int("queue", 128, "reconcile queue size")
		logFile = flag.String("logfile", "", "structured log file (default $DATA/logs/fieldmerged.jsonl)")
	)
	flag.Parse()

	if err := os.MkdirAll(filepath.Join(*dataDir, "logs"), 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "mkdir data: %v\n", err)
		os.Exit(1)
	}
	logPath := *logFile
	if logPath == "" {
		logPath = filepath.Join(*dataDir, "logs", "fieldmerged.jsonl")
	}
	lf, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open log: %v\n", err)
		os.Exit(1)
	}
	defer lf.Close()
	logger := logx.New(logx.MultiWriter(os.Stderr, lf), "server")

	dbPath := filepath.Join(*dataDir, "fieldmerge.db")
	st, err := store.Open(dbPath, store.DefaultLimits)
	if err != nil {
		logger.Error("start_db_failed", map[string]any{"err": err.Error()})
		os.Exit(1)
	}
	defer st.Close()

	ad := &adapter.FileAdapter{RootDir: filepath.Join(*dataDir, "applied")}
	rcfg := reconcile.DefaultConfig()
	rcfg.Workers = *workers
	rcfg.QueueSize = *queue
	loop := reconcile.New(st, ad, rcfg, logger.With(map[string]any{"component": "reconcile"}))

	ctx, cancel := signalContext()
	defer cancel()
	loop.Start(ctx)

	srv := server.New(st, loop, logger.With(map[string]any{"component": "http"}), server.Options{})
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Info("server_listening", map[string]any{
			"addr": *addr, "db": dbPath, "adapter": ad.Name(),
			"workers": *workers, "queue": *queue,
		})
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("http_failed", map[string]any{"err": err.Error()})
			cancel()
		}
	}()

	<-ctx.Done()
	logger.Info("server_shutting_down", nil)
	shCtx, shCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shCancel()
	_ = httpSrv.Shutdown(shCtx)
	loop.Stop()
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
