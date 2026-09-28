// Command server runs the local interruption-budget eviction coordinator with
// synthetic observation/failure inputs and a SQLite database.
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/local/evictioncoordinator/internal/config"
	"github.com/local/evictioncoordinator/internal/coordinator"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/fixture"
	"github.com/local/evictioncoordinator/internal/httpapi"
	"github.com/local/evictioncoordinator/internal/store"
)

func main() {
	configPath := flag.String("config", "configs/local.json", "path to config JSON")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	cfg, err := config.Load(*configPath)
	if err != nil {
		logger.Error("config load failed", "err", err)
		os.Exit(2)
	}

	if cfg.DatabasePath != ":memory:" {
		if err := os.MkdirAll(filepath.Dir(cfg.DatabasePath), 0o755); err != nil {
			logger.Error("create data dir", "err", err)
			os.Exit(1)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.DatabasePath)
	if err != nil {
		logger.Error("open store", "err", err)
		os.Exit(1)
	}
	defer st.Close()

	clock := time.Now
	coord := coordinator.New(st, coordinator.Config{
		ApprovalTTL: cfg.ApprovalTTL(),
		Clock:       clock,
	})

	// The synthetic adapter is the local stand-in for a cluster's node/kubelet
	// monitors. In a real deployment these two callbacks would be backed by
	// informers; here they persist scripted facts.
	ad := fixture.NewAdapter(st, clock)

	srv := http.Server{
		Addr: cfg.HTTPAddr,
		Handler: httpapi.NewServer(httpapi.Deps{
			Store:       st,
			Coordinator: coord,
			EmitObservation: func(ctx context.Context, inst domain.Instance, ready bool, epoch int64) error {
				return ad.EmitObservation(ctx, inst, ready, epoch)
			},
			EmitFailure: ad.EmitFailure,
			Logger:      logger,
		}).Handler,
		ReadHeaderTimeout: 5 * time.Second,
	}

	loopCtx, cancelLoop := context.WithCancel(ctx)
	results := coord.StartLoop(loopCtx, cfg.SweepInterval())
	go func() {
		for res := range results {
			if len(res.Expired) > 0 {
				logger.Info("approval expiries swept (slots still charged until reclaimed)",
					"count", len(res.Expired), "at", res.At.Format(time.RFC3339))
			}
		}
	}()

	go func() {
		logger.Info("eviction coordinator listening", "addr", cfg.HTTPAddr, "db", cfg.DatabasePath)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http server", "err", err)
			stop()
		}
	}()

	<-ctx.Done()
	logger.Info("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cancelLoop()
	_ = srv.Shutdown(shutdownCtx)
	logger.Info("stopped cleanly")
}
