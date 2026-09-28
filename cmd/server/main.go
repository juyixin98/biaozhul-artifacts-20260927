// Command netpol-server runs the offline container network reachability
// backend: it loads synthetic desired state, reconciles it into revisioned
// SQLite snapshots, and serves allow/deny decisions over HTTP.
package main

import (
	"context"
	"errors"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"netpolicy/internal/adapter"
	"netpolicy/internal/config"
	"netpolicy/internal/diag"
	"netpolicy/internal/engine"
	"netpolicy/internal/reconcile"
	"netpolicy/internal/source"
	"netpolicy/internal/store"
)

func main() {
	cfgPath := flag.String("config", "configs/config.json", "path to configuration file")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		// Logger is not up yet; plain stderr is appropriate.
		os.Stderr.WriteString("configuration error: " + err.Error() + "\n")
		os.Exit(2)
	}
	log := diag.NewLogger(cfg.LogLevel, os.Stdout)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.SQLite.DSN)
	if err != nil {
		log.Error("open store", "error", err)
		os.Exit(1)
	}
	defer st.Close()

	src := &source.FixtureSource{Path: cfg.FixturePath}
	rec := reconcile.New(src, st)
	loop := reconcile.NewLoop(rec, cfg.ReconcileInterval.Duration, log)

	holder := &adapter.Holder{}
	srv := &adapter.Server{Holder: holder, Store: st, Loop: loop, Logger: log}

	// Initial reconciliation before serving, then install the engine.
	if err := runInitial(ctx, rec, st, holder, log); err != nil {
		log.Error("initial reconciliation", "error", err)
		os.Exit(1)
	}
	if trimmed, err := st.TrimHistory(ctx, cfg.HistoryKeep); err != nil {
		log.Warn("trim snapshot history", "error", err)
	} else if trimmed > 0 {
		log.Info("trimmed old snapshots", "removed", trimmed)
	}

	httpSrv := &http.Server{
		Addr:              cfg.HTTP.Addr,
		Handler:           srv.NewRouter(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	loopDone := make(chan struct{})
	go func() {
		defer close(loopDone)
		loop.Run(ctx)
	}()
	// Keep the served engine aligned with the newest persisted revision.
	go trackRevisions(ctx, holder, st, log)

	go func() {
		log.Info("http server listening", "addr", cfg.HTTP.Addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	log.Info("shutdown requested")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Error("graceful shutdown", "error", err)
	}
	<-loopDone
}

func runInitial(ctx context.Context, rec *reconcile.Reconciler, st *store.Store, holder *adapter.Holder, log interface {
	Info(string, ...any)
	Warn(string, ...any)
	Error(string, ...any)
}) error {
	r, err := rec.Once(ctx)
	if err != nil {
		return err
	}
	if r.Status == reconcile.StatusFetchFailed || r.Status == reconcile.StatusValidationFailed {
		// Fatal at boot: an offline backend with no usable fixture must not
		// silently serve "no snapshot". Report the classified cause.
		os.Stderr.WriteString("initial reconciliation failed: " + r.ErrorKind + ": " + r.ErrorMessage + "\n")
		os.Exit(1)
	}
	snap, err := st.Latest(ctx)
	if err != nil {
		return err
	}
	if snap != nil {
		holder.Set(engine.New(snap))
		log.Info("initial snapshot loaded", "revision", snap.Revision, "status", r.Status)
	}
	return nil
}

// trackRevisions polls for newly persisted revisions and swaps the engine.
// The poll is cheap (single indexed SELECT) and keeps the loop and the
// serving engine decoupled: every served engine corresponds to one
// successfully committed transaction.
func trackRevisions(ctx context.Context, holder *adapter.Holder, st *store.Store, log interface{ Info(string, ...any) }) {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			snap, err := st.Latest(ctx)
			if err != nil || snap == nil {
				continue
			}
			if cur := holder.Engine(); cur != nil && cur.Revision() == snap.Revision {
				continue
			}
			holder.Set(engine.New(snap))
			log.Info("activated new revision", "revision", snap.Revision)
		}
	}
}
