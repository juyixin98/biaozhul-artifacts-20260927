// Package app wires the modules together: SQLite store + fleet actuator,
// configuration seeding, the controller engine, its background tick loop and
// the HTTP adapter. Both cmd/replicactl and tests construct the system
// through this single builder so tests exercise the real assembly.
package app

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"time"

	"replicactl/internal/adapter"
	"replicactl/internal/config"
	"replicactl/internal/controller"
	"replicactl/internal/model"
	"replicactl/internal/store"
)

// Options configures a constructed application.
type Options struct {
	DSN     string         // SQLite DSN; defaults to file:replicactl.db
	Config  *config.Config // initial config; defaults to config.Default()
	Logger  *slog.Logger
	Clock   controller.Clock // injectable for deterministic tests
	// AutoReconcile starts the background cadence loop when > 0.
	AutoReconcile time.Duration
}

// App is the running system.
type App struct {
	Store    *store.Store
	Config   config.Config
	Server   *http.Server
	Handler  http.Handler
	Engine   *controller.Engine
	HTTP     *adapter.Server
	log      *slog.Logger
	cancel   context.CancelFunc
	revision int64
}

// New constructs and seeds the application without starting the server.
func New(ctx context.Context, opt Options) (*App, error) {
	if opt.Logger == nil {
		opt.Logger = slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	}
	if opt.Clock == nil {
		opt.Clock = controller.WallClock{}
	}
	now := opt.Clock.Now

	st, err := store.New(ctx, opt.DSN)
	if err != nil {
		return nil, err
	}
	cfg := config.Default()
	if opt.Config != nil {
		cfg = *opt.Config
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	rev, err := st.SaveConfig(ctx, cfg, now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return nil, err
	}
	// Seeding only creates the initial fleet when none exists; a restart is a
	// no-op and preserves the persisted replica count.
	if err := st.SeedFleet(ctx, cfg.InitialReplicas, now()); err != nil {
		return nil, err
	}

	eng := controller.NewEngine(st, opt.Clock)
	httpNow := func() time.Time { return opt.Clock.Now() }
	srv := adapter.NewServer(eng, st, opt.Logger, httpNow)
	handler := srv.Handler()

	a := &App{
		Store:    st,
		Config:   cfg,
		Engine:   eng,
		HTTP:     srv,
		Handler:  handler,
		Server:   &http.Server{Addr: cfg.ListenAddr, Handler: handler},
		log:      opt.Logger,
		revision: rev,
	}

	if opt.AutoReconcile > 0 {
		bg, cancel := context.WithCancel(context.Background())
		a.cancel = cancel
		go a.loop(bg, opt.AutoReconcile, now)
	}
	return a, nil
}

// loop runs the background reconciliation cadence.
func (a *App) loop(ctx context.Context, every time.Duration, now func() time.Time) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			rid := newRequestID()
			d, err := a.Engine.Reconcile(ctx, rid)
			if err != nil {
				a.log.Error("auto_reconcile_failed", "request_id", rid, "error", err)
				continue
			}
			a.HTTP.LogDecisionFor(d)
		}
	}
}

// Reconcile exposes one tick for tests/manual drivers.
func (a *App) Reconcile(ctx context.Context, rid string) (*model.Decision, error) {
	return a.Engine.Reconcile(ctx, rid)
}

// Close stops the background loop and closes the database.
func (a *App) Close() error {
	if a.cancel != nil {
		a.cancel()
	}
	return a.Store.Close()
}
