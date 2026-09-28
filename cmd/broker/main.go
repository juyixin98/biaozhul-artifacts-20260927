// Command broker runs the local work-message broker HTTP server.
//
// Backends:
//
//	BROKER_BACKEND=memory   (default) in-process state, dies with the process
//	BROKER_BACKEND=postgres persistent PostgreSQL at BROKER_POSTGRES_DSN
//
// Every startup prints run identity and versions so logs are traceable.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"workbroker/internal/broker"
	"workbroker/internal/clock"
	"workbroker/internal/config"
	"workbroker/internal/httpapi"
	"workbroker/internal/kernel"
	"workbroker/internal/store"
	"workbroker/internal/version"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("broker: %v", err)
	}
}

func run() error {
	conf, err := config.FromEnv()
	if err != nil {
		return err
	}
	if conf.RunID == "" {
		conf.RunID = kernel.NewID("run-")
	}
	clk := clock.NewReal()
	logger := log.New(os.Stdout, "broker ", log.LstdFlags|log.Lmicroseconds)

	var st store.Store
	switch conf.Backend {
	case "memory":
		st = store.NewMemory(clk)
		logger.Printf("run_id=%s backend=memory service=%s proto=%s schema=%d",
			conf.RunID, version.Service, version.ProtocolVersion, version.SchemaVersion)
	case "postgres":
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		pg, err := store.OpenPostgres(ctx, conf.PostgresDSN, clk)
		cancel()
		if err != nil {
			return fmt.Errorf("postgres backend: %w", err)
		}
		st = pg
		logger.Printf("run_id=%s backend=postgres dsn=%q service=%s proto=%s schema=%d",
			conf.RunID, conf.PostgresDSN, version.Service, version.ProtocolVersion, version.SchemaVersion)
	default:
		return fmt.Errorf("unknown backend %q", conf.Backend)
	}
	defer func() { _ = st.Close() }()

	svc := broker.New(st, conf)
	srv := &http.Server{
		Addr:              conf.HTTPAddr,
		Handler:           httpapi.NewHandler(svc, logger),
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	serveErr := make(chan error, 1)
	go func() {
		logger.Printf("listening on %s (run_id=%s)", conf.HTTPAddr, conf.RunID)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
		}
	}()

	select {
	case err := <-serveErr:
		return err
	case sig := <-stop:
		logger.Printf("signal %s received, shutting down (run_id=%s)", sig, conf.RunID)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return srv.Shutdown(ctx)
	}
}
