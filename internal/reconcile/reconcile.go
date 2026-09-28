// Package reconcile runs the control loop that observes pending instances
// in the store, asks the scheduler for a feasible batch placement and
// commits the outcome.
//
// Error handling is deliberately explicit:
//
//   - feasible run        -> decisions committed, run logged as "feasible"
//   - constraint conflict -> nothing committed; attempts incremented and
//     instances flipped to "failed" once the retry
//     budget is exhausted; run logged as "conflict"
//   - infrastructure error (store/serialization) -> returned to the
//     caller, logged at error level, never reported
//     as success
package reconcile

import (
	"context"
	"sync"
	"time"

	"placer/internal/config"
	"placer/internal/logx"
	"placer/internal/model"
	"placer/internal/scheduler"
	"placer/internal/store"
)

// Loop is one reconciliation loop bound to a store.
type Loop struct {
	st      *store.Store
	cfg     config.PlacementDefaults
	maxRetr int
	log     *logx.Logger

	mu      sync.Mutex
	running bool
	stop    chan struct{}
	done    chan struct{}
}

// New creates a loop.
func New(st *store.Store, cfg config.Config, log *logx.Logger) *Loop {
	return &Loop{
		st:      st,
		cfg:     cfg.DefaultPlacement,
		maxRetr: cfg.MaxRetries,
		log:     log,
	}
}

// RunOnce performs exactly one reconciliation pass and returns a summary.
func (l *Loop) RunOnce(ctx context.Context, runID string) (Summary, error) {
	if runID == "" {
		runID = logx.NewRunID()
	}
	log := l.log.With("reconcile", runID)
	log.Info("reconcile_start", map[string]any{})

	snap, err := l.st.LoadSnapshot(ctx)
	if err != nil {
		log.Error("snapshot_failed", err, nil)
		return Summary{RunID: runID, Status: StatusError}, err
	}
	log.Info("snapshot_loaded", map[string]any{
		"nodes": len(snap.Nodes), "pending": len(snap.Pending), "bound": len(snap.Bound),
	})

	sum := Summary{RunID: runID, Nodes: len(snap.Nodes), Pending: len(snap.Pending)}
	if len(snap.Pending) == 0 {
		sum.Status = StatusIdle
		log.Info("reconcile_idle", nil)
		_ = l.st.SaveRun(ctx, runID, "reconcile", string(StatusIdle),
			map[string]any{"nodes": len(snap.Nodes)}, map[string]any{"decisions": []model.Decision{}})
		return sum, nil
	}

	opts := model.PlanOptions{
		SpreadTopologyKey: l.cfg.SpreadTopologyKey,
		SkewDomainMode:    l.cfg.SkewDomainMode,
		MaxNodesForExact:  l.cfg.MaxPendingForExact,
		SearchBudget:      l.cfg.SearchBudget,
	}
	include := l.cfg.IncludeEmptyDomains
	opts.IncludeEmptyDomains = &include

	req := model.PlanRequest{
		RunID: runID, Nodes: snap.Nodes, Instances: snap.Pending,
		Bound: snap.Bound, Policy: snap.Policy, Options: opts,
	}
	res, err := scheduler.Plan(req)
	if err != nil {
		// Input validation or search-budget exhaustion: an error, not a
		// conflict — do not mutate instance state.
		log.Error("scheduler_error", err, map[string]any{"pending": len(snap.Pending)})
		_ = l.st.SaveRun(ctx, runID, "reconcile", string(StatusError), req,
			map[string]any{"err": err.Error()})
		sum.Status = StatusError
		return sum, err
	}

	if res.Feasible {
		if err := l.st.CommitBindings(ctx, runID, res.Decisions); err != nil {
			log.Error("commit_failed", err, map[string]any{"decisions": len(res.Decisions)})
			_ = l.st.SaveRun(ctx, runID, "reconcile", string(StatusError), req,
				map[string]any{"err": err.Error()})
			sum.Status = StatusError
			return sum, err
		}
		sum.Status = StatusFeasible
		sum.Decisions = res.Decisions
		sum.Objective = res.Objective
		sum.Solver = res.Solver
		if err := l.st.SaveRun(ctx, runID, "reconcile", string(StatusFeasible), req, res); err != nil {
			log.Error("run_persist_failed", err, nil)
		}
		log.Info("reconcile_feasible", map[string]any{
			"decisions": len(res.Decisions), "solver": res.Solver,
			"objective": res.Objective,
		})
		return sum, nil
	}

	// Conflict path: retry accounting. Instances past the budget become
	// "failed"; the rest remain pending for a later pass.
	terminal := map[string]bool{}
	var retrying []model.Conflict
	for _, c := range res.Conflicts {
		n, err := l.st.Attempts(ctx, c.InstanceID)
		if err != nil {
			log.Error("attempts_lookup_failed", err, map[string]any{"instance_id": c.InstanceID})
			sum.Status = StatusError
			return sum, err
		}
		if n+1 >= l.maxRetr {
			terminal[c.InstanceID] = true
		} else {
			retrying = append(retrying, c)
		}
	}
	if len(retrying) > 0 {
		if err := l.st.MarkFailed(ctx, runID, retrying, false); err != nil {
			log.Error("retry_record_failed", err, nil)
			sum.Status = StatusError
			return sum, err
		}
	}
	var termList []model.Conflict
	for _, c := range res.Conflicts {
		if terminal[c.InstanceID] {
			termList = append(termList, c)
		}
	}
	if len(termList) > 0 {
		if err := l.st.MarkFailed(ctx, runID, termList, true); err != nil {
			log.Error("terminal_failure_record_failed", err, nil)
			sum.Status = StatusError
			return sum, err
		}
	}
	sum.Status = StatusConflict
	sum.Conflicts = res.Conflicts
	if err := l.st.SaveRun(ctx, runID, "reconcile", string(StatusConflict), req, res); err != nil {
		log.Error("run_persist_failed", err, nil)
	}
	log.Info("reconcile_conflict", map[string]any{
		"conflicts": len(res.Conflicts), "terminal": len(termList),
		"retrying": len(retrying), "codes": codesOf(res.Conflicts),
	})
	return sum, nil
}

func codesOf(cs []model.Conflict) []string {
	out := make([]string, 0, len(cs))
	for _, c := range cs {
		out = append(out, string(c.Code))
	}
	return out
}

// Status values for Summary and the runs table.
type Status string

const (
	StatusIdle     Status = "idle"
	StatusFeasible Status = "feasible"
	StatusConflict Status = "conflict"
	StatusError    Status = "error"
)

// Summary is the outcome of one pass.
type Summary struct {
	RunID     string
	Status    Status
	Nodes     int
	Pending   int
	Decisions []model.Decision
	Conflicts []model.Conflict
	Objective *model.Objective
	Solver    string
}

// Start launches the periodic loop. Interval <= 0 means manual-only.
func (l *Loop) Start(ctx context.Context, intervalSec int) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.running {
		return nil
	}
	l.running = true
	l.stop = make(chan struct{})
	l.done = make(chan struct{})
	if intervalSec <= 0 {
		// Manual-only mode: mark as not running but keep channels nil-safe.
		l.running = false
		return nil
	}
	go l.tick(ctx, time.Duration(intervalSec)*time.Second)
	return nil
}

func (l *Loop) tick(parent context.Context, interval time.Duration) {
	defer close(l.done)
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-parent.Done():
			return
		case <-l.stop:
			return
		case <-t.C:
			if _, err := l.RunOnce(parent, ""); err != nil {
				// Error already logged inside RunOnce; the loop survives.
				continue
			}
		}
	}
}

// Stop halts the periodic goroutine.
func (l *Loop) Stop() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.running {
		return
	}
	close(l.stop)
	l.running = false
}
