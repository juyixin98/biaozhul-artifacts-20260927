// Package controller implements the reconcile loop: the rolling-update state
// machine. It is the only place that encodes policy (surge/unavailable budget,
// readiness threshold, start-failure budget, progress deadline); persistence
// is delegated to store and process lifecycle to adapter.
//
// One reconcile step ("tick") per workload performs, in order:
//
//  1. observation housekeeping (poll processes, reap exited ones, promote
//     readiness, record jitter) — never changes the serving count;
//  2. at most one structural action (start one new instance or terminate one
//     old instance);
//  3. release completion / failure accounting.
//
// The single structural action per tick makes every rollout a deterministic,
// step-by-step sequence that tests can assert exactly.
package controller

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"rollctl/internal/adapter"
	"rollctl/internal/model"
	"rollctl/internal/store"
)

// Sentinel errors for user-facing operations.
var (
	ErrActiveRelease    = errors.New("controller: a release is already pending or active")
	ErrNoRollbackTarget = errors.New("controller: no earlier succeeded revision to roll back to")
	ErrWorkloadNotFound = errors.New("controller: workload not found")
	ErrReleaseNotFound  = errors.New("controller: release not found")
	ErrBadRequest       = errors.New("controller: invalid request")
)

// Options configures a Controller.
type Options struct {
	Logger *slog.Logger
	// Clock returns wall-clock time for rows/events; tests may inject a fake.
	Clock func() time.Time
}

// Controller runs the reconcile loop and owns the mutating API operations.
type Controller struct {
	mu   sync.Mutex
	st   *store.Store
	pm   adapter.ProcessManager
	log  *slog.Logger
	now  func() time.Time
	tick int64
}

// New constructs a controller and reloads the persisted logical tick so a
// restarted controller continues at the next step.
func New(ctx context.Context, st *store.Store, pm adapter.ProcessManager, opts Options) (*Controller, error) {
	t, err := st.Tick(ctx)
	if err != nil {
		return nil, err
	}
	c := &Controller{
		st:   st,
		pm:   pm,
		log:  opts.Logger,
		now:  opts.Clock,
		tick: t,
	}
	if c.log == nil {
		c.log = slog.Default()
	}
	if c.now == nil {
		c.now = func() time.Time { return time.Now().UTC() }
	}
	return c, nil
}

func genID(prefix string) string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return prefix + "-" + hex.EncodeToString(b[:])
}

// WorkloadTick is the per-workload result of one reconcile step. Counts are
// computed AFTER observation and the structural action, so tests can assert
// the replica constraints at every step.
type WorkloadTick struct {
	Workload      string         `json:"workload"`
	Baseline      int            `json:"baseline"`
	Live          int            `json:"live"`
	Available     int            `json:"available"`
	NewLive       int            `json:"newLive"`
	NewAvailable  int            `json:"newAvailable"`
	OldLive       int            `json:"oldLive"`
	OldAvailable  int            `json:"oldAvailable"`
	ActiveRelease string         `json:"activeReleaseId,omitempty"`
	Events        []*model.Event `json:"events"`
}

// TickResult is the full result of one reconcile step across all workloads.
type TickResult struct {
	Tick      int64          `json:"tick"`
	Workloads []WorkloadTick `json:"workloads"`
}

// CurrentTick returns the persisted logical tick without reconciling.
func (c *Controller) CurrentTick() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tick
}

// Tick advances the logical clock by one and reconciles every workload.
func (c *Controller) Tick(ctx context.Context) (TickResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.tick++
	t := c.tick
	if err := c.st.SetTick(ctx, t); err != nil {
		return TickResult{}, err
	}
	if td, ok := c.pm.(adapter.TickDriven); ok {
		td.SetTick(t)
	}

	names, err := c.st.ListWorkloads(ctx)
	if err != nil {
		return TickResult{}, err
	}
	res := TickResult{Tick: t}
	for _, name := range names {
		wt, err := c.reconcileWorkload(ctx, name, t)
		if err != nil {
			return res, fmt.Errorf("reconcile %s at tick %d: %w", name, t, err)
		}
		res.Workloads = append(res.Workloads, wt)
	}
	return res, nil
}

// Run drives ticks every interval until the context is canceled.
func (c *Controller) Run(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if _, err := c.Tick(ctx); err != nil {
				c.log.Error("reconcile step failed", "tick", c.CurrentTick(), "err", err)
			}
		}
	}
}

// ---------------------------------------------------------------- snapshots

// InstanceView is the API representation of an instance.
type InstanceView = model.Instance

// WorkloadStatus is the API snapshot of a workload and its fleet.
type WorkloadStatus struct {
	Workload        *model.Workload   `json:"workload"`
	Baseline        int               `json:"baseline"`
	Live            int               `json:"live"`
	Available       int               `json:"available"`
	ByRevision      map[string]RevCnt `json:"byRevision"`
	ActiveReleaseID string            `json:"activeReleaseId,omitempty"`
	Instances       []*model.Instance `json:"instances"`
}

// RevCnt is a live/available pair keyed by revision.
type RevCnt struct {
	Live      int `json:"live"`
	Available int `json:"available"`
}

// ListWorkloads lists all workload names.
func (c *Controller) ListWorkloads(ctx context.Context) ([]string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.st.ListWorkloads(ctx)
}

// Status reads a workload snapshot.
func (c *Controller) Status(ctx context.Context, name string) (*WorkloadStatus, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	w, err := c.st.GetWorkload(ctx, name)
	if errors.Is(err, store.ErrNotFound) {
		return nil, ErrWorkloadNotFound
	}
	if err != nil {
		return nil, err
	}
	insts, err := c.st.ListInstances(ctx, name)
	if err != nil {
		return nil, err
	}
	st := &WorkloadStatus{Workload: w, Baseline: w.Replicas, ByRevision: map[string]RevCnt{}, Instances: insts}
	for _, ins := range insts {
		rc := st.ByRevision[ins.Revision]
		if ins.Live() {
			rc.Live++
			st.Live++
		}
		if ins.Available() {
			rc.Available++
			st.Available++
		}
		st.ByRevision[ins.Revision] = rc
	}
	if rel, _ := c.st.ActiveRelease(ctx, name); rel != nil {
		st.ActiveReleaseID = rel.ID
	}
	return st, nil
}

// Release reads one release.
func (c *Controller) Release(ctx context.Context, id string) (*model.Release, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, err := c.st.GetRelease(ctx, id)
	if errors.Is(err, store.ErrNotFound) {
		return nil, ErrReleaseNotFound
	}
	return r, err
}

// Releases lists a workload's full release history (newest first).
func (c *Controller) Releases(ctx context.Context, workload string) ([]*model.Release, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.st.GetWorkload(ctx, workload); err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, ErrWorkloadNotFound
		}
		return nil, err
	}
	return c.st.ListReleases(ctx, workload)
}

// Events returns workload events after sequence cursor (0 = all).
func (c *Controller) Events(ctx context.Context, workload string, afterSeq int64) ([]*model.Event, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.st.EventsAfter(ctx, workload, afterSeq)
}
