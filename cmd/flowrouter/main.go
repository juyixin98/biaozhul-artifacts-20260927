// Command flowrouter runs the local five-tuple to next-hop elastic hashing
// routing service.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"flowrouter/internal/api"
	"flowrouter/internal/apperr"
	"flowrouter/internal/config"
	"flowrouter/internal/health"
	"flowrouter/internal/replay"
	"flowrouter/internal/router"
	"flowrouter/internal/store"
)

func main() {
	var (
		configPath  = flag.String("config", "configs/config.example.yaml", "path to YAML config")
		flowsetDir  = flag.String("flowsets", "testdata/flowsets", "directory of flow set JSON fixtures")
		checkConfig = flag.Bool("check-config", false, "validate config and exit (0 ok, 2 invalid)")
		resetDB     = flag.Bool("reset-db", false, "delete the SQLite database before starting")
	)
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	cfg, err := config.Load(*configPath)
	if err != nil {
		fatal(logger, "config", err)
	}
	if *checkConfig {
		logger.Info("config valid", "path", *configPath, "members", len(cfg.Members),
			"vnodes_per_weight", cfg.VNodesPerWeight, "max_vnodes", cfg.MaxVNodes)
		return
	}

	if *resetDB && cfg.Store.Path != ":memory:" {
		if err := os.Remove(cfg.Store.Path); err != nil && !errors.Is(err, os.ErrNotExist) {
			logger.Error("remove db", "err", err)
			os.Exit(1)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, cfg.Store.Path, cfg.Store.BusyTimeoutMs)
	if err != nil {
		fatal(logger, "store open", err)
	}
	defer st.Close()

	rt := router.New(cfg.VNodesPerWeight, cfg.MaxVNodes)
	srv := &api.Server{
		RT: rt, Store: st, Config: cfg, ConfigPath: *configPath,
		Library: replay.NewFileLibrary(*flowsetDir),
		Planner: replay.NewPlanner(st, cfg.VNodesPerWeight, cfg.MaxVNodes),
		Logger:  logger,
	}

	if err := bootstrap(ctx, srv, cfg, *configPath, logger); err != nil {
		fatal(logger, "bootstrap", err)
	}

	var prober *health.Prober
	if cfg.Health.Enabled {
		prober, err = health.New(rt, cfg.Health.Interval.Duration, cfg.Health.Timeout.Duration,
			cfg.Health.Failures, logger)
		if err != nil {
			fatal(logger, "prober", err)
		}
		prober.Start()
		defer prober.Stop()
	}

	httpSrv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.NewMux(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Info("listening", "addr", cfg.Listen, "version", rt.Current().Version,
			"flowsets", *flowsetDir)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http serve", "err", err)
			stop()
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shCtx)
}

// bootstrap either restores the newest persisted generation or performs the
// initial load from the YAML file and persists generation 1.
func bootstrap(ctx context.Context, srv *api.Server, cfg *config.Config, path string, logger *slog.Logger) error {
	maxV, err := srv.Store.MaxRingVersion(ctx)
	if err != nil {
		return err
	}
	if maxV > 0 {
		row, err := srv.Store.RingVersion(ctx, maxV)
		if err != nil {
			return err
		}
		var infos []router.MemberInfo
		if err := json.Unmarshal(row.MembersJSON, &infos); err != nil {
			return apperr.Compute("VERSION_ROW_CORRUPT",
				fmt.Sprintf("persisted version %d members JSON unparsable", maxV)).WithCause(err)
		}
		members := make([]config.Member, 0, len(infos))
		down := map[string]bool{}
		reasons := map[string]string{}
		for _, mi := range infos {
			members = append(members, config.Member{ID: mi.ID, Address: mi.Address, Weight: mi.Weight})
			if !mi.Up {
				down[mi.ID] = true
				reasons[mi.ID] = mi.DownReason
			}
		}
		snap, err := srv.RT.Restore(members, down, reasons, row.Fingerprint, row.Version)
		if err != nil {
			return err
		}
		logger.Info("restored generation from sqlite", "version", snap.Version,
			"members", snap.NumMembers, "on_ring", snap.NumOnRing)
		return nil
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		return apperr.Invalid("CONFIG_UNREADABLE",
			fmt.Sprintf("cannot read config file %q", path)).WithCause(err)
	}
	sum := sha256.Sum256(raw)
	sha := hex.EncodeToString(sum[:])
	down := map[string]bool{}
	snap, changed, err := srv.RT.Load(cfg.Members, down)
	if err != nil {
		return err
	}
	if !changed {
		// Empty config still produces generation 1 so versioning starts.
		logger.Info("initial member set is empty; routing will return NO_HEALTHY_MEMBER until configured")
	}
	if err := srv.PersistInitial(ctx, raw, path, sha, snap); err != nil {
		return err
	}
	logger.Info("initial load persisted", "version", snap.Version, "sha256", sha)
	return nil
}

func fatal(logger *slog.Logger, stage string, err error) {
	if ae, ok := apperr.As(err); ok {
		logger.Error(stage, "kind", ae.Kind, "code", ae.Code, "message", ae.Message, "cause", ae.Cause)
	} else {
		logger.Error(stage, "err", err)
	}
	os.Exit(1)
}
