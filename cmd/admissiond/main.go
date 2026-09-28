// Command admissiond starts the local admission service: it loads the
// configuration, opens SQLite, builds the ordered chain, starts the bounded
// reconcile loop and serves the AdmissionReview HTTP API with graceful
// shutdown.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"admission/internal/config"
	"admission/internal/httpapi"
	"admission/internal/reconcile"
	"admission/internal/runlogger"
	"admission/internal/service"
	"admission/internal/storage"
)

func main() {
	cfgPath := flag.String("config", "configs/admissiond.json", "path to configuration file")
	flag.Parse()

	if err := run(*cfgPath); err != nil {
		fmt.Fprintf(os.Stderr, "admissiond: %v\n", err)
		os.Exit(1)
	}
}

func run(cfgPath string) error {
	f, err := config.Load(cfgPath)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	store, err := storage.Open(ctx, f.Storage.SQLitePath)
	if err != nil {
		return err
	}
	defer store.Close()

	logger := runlogger.New(f.Logging.Dir)
	built, err := config.Build(f, logger)
	if err != nil {
		return err
	}

	svc, err := service.New(service.Deps{
		Pipeline: built.Pipeline,
		Store:    store,
		Ledger:   built.Ledger,
	})
	if err != nil {
		return err
	}

	rec := reconcile.New(svc, store, reconcile.Policy{
		MaxAttempts: f.Reconcile.MaxAttempts,
		BaseDelay:   time.Duration(f.Reconcile.BaseDelayMS) * time.Millisecond,
		Factor:      f.Reconcile.Factor,
		MaxDelay:    time.Duration(f.Reconcile.MaxDelayMS) * time.Millisecond,
	}, nil, nil)

	go rec.Start(ctx, time.Duration(f.Reconcile.TickMS)*time.Millisecond)

	srv := httpapi.NewServer(svc, rec, logger).
		WithWires(store.RecentAudit, store.CountRetry)

	httpServer := &http.Server{
		Addr:         f.HTTP.Addr,
		Handler:      srv.Handler(),
		ReadTimeout:  time.Duration(f.HTTP.ReadTimeoutMS) * time.Millisecond,
		WriteTimeout: time.Duration(f.HTTP.WriteTimeoutMS) * time.Millisecond,
	}

	errCh := make(chan error, 1)
	go func() {
		fmt.Printf("admissiond listening on %s (sqlite=%s, logs=%s)\n",
			f.HTTP.Addr, f.Storage.SQLitePath, f.Logging.Dir)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
		close(errCh)
	}()

	select {
	case <-ctx.Done():
	case err := <-errCh:
		if err != nil {
			return err
		}
	}

	shCtx, cancel := context.WithTimeout(context.Background(),
		time.Duration(f.HTTP.ShutdownTimeoutMS)*time.Millisecond)
	defer cancel()
	return httpServer.Shutdown(shCtx)
}
