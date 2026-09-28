// Package reconcile implements the reconciliation loop: it compares a desired
// set of compute instances (identity = stable id with a revision) against the
// running set persisted for a cluster, computes a placement plan for additions
// and rolling replacements, and applies it transactionally — a replaced
// instance is evicted only AFTER its new copy has been placed successfully.
package reconcile

import (
	"context"
	"fmt"
	"sort"
	"time"

	"opp284/placement/internal/engine"
	"opp284/placement/internal/logging"
	"opp284/placement/internal/model"
	"opp284/placement/internal/scheduler"
	"opp284/placement/internal/store"
)

// DesiredInstance is one entry of the declared desired state.
type DesiredInstance struct {
	ID           string          // stable logical identity
	Revision     string          // changes when the instance must be replaced
	Request      model.Resources
	RequiredZone string
	NodeSelector model.Selector
	Groups       []string
}

// DesiredProvider supplies the desired state for one reconciliation.
// Implementations in this repo are local/synthetic (static provider and
// JSON-file provider); real cloud adapters are intentionally out of scope.
type DesiredProvider interface {
	Desired(ctx context.Context, clusterID string) ([]DesiredInstance, error)
}

// Result reports one reconcile tick.
type Result struct {
	RunID     string
	Status    string // ok | failed | degraded
	Detail    string
	Plans     []string // plan ids produced (succeeded)
	Failures  []string // plan ids produced (constraint conflicts)
	Evicted   []string // old running instance ids evicted
}

// Reconciler runs one tick at a time and optionally on a ticker.
type Reconciler struct {
	eng             *engine.Engine
	st              *store.Store
	provider        DesiredProvider
	allowRecreate   bool
	maxPlansPerTick int
	log             *logging.Logger
}

// New builds a reconciler.
func New(eng *engine.Engine, st *store.Store, p DesiredProvider,
	allowRecreate bool, maxPlansPerTick int, log *logging.Logger) *Reconciler {
	if maxPlansPerTick <= 0 {
		maxPlansPerTick = 10
	}
	return &Reconciler{
		eng: eng, st: st, provider: p,
		allowRecreate: allowRecreate, maxPlansPerTick: maxPlansPerTick,
		log: log,
	}
}

// diffItem is the classification of one desired-vs-running pair.
type diffItem struct {
	desired DesiredInstance
	kind    string // add | replace | unchanged
	old     *store.RunningRecord
}

// Tick performs exactly one reconciliation pass.
func (r *Reconciler) Tick(ctx context.Context, clusterID, requestID string) (*Result, error) {
	runID := "rc-" + engine.NewRequestID()[3:]
	l := r.log.With(map[string]any{"request_id": requestID, "run_id": runID, "cluster_id": clusterID})
	res := &Result{RunID: runID}

	desired, err := r.provider.Desired(ctx, clusterID)
	if err != nil {
		res.Status, res.Detail = "failed", "desired provider: "+err.Error()
		l.Fail("fetch desired state", map[string]any{"err": err.Error()})
		_ = r.st.RecordReconcileRun(ctx, runID, requestID, res.Status, res.Detail)
		return res, err
	}
	_, running, _, _, err := r.st.LoadCluster(ctx, clusterID)
	if err != nil {
		res.Status, res.Detail = "failed", "load cluster: "+err.Error()
		l.Fail("load cluster", map[string]any{"err": err.Error()})
		_ = r.st.RecordReconcileRun(ctx, runID, requestID, res.Status, res.Detail)
		return res, err
	}

	items, removals := diff(desired, running)
	l.Debug("reconcile diff", map[string]any{
		"add":      countKind(items, "add"),
		"replace":  countKind(items, "replace"),
		"unchanged": countKind(items, "unchanged"),
		"remove":   len(removals),
	})

	// Plan per item individually (one new/replaced identity per plan) so the
	// old copy's reservation is held precisely for its own replacement and
	// failure attribution stays per-instance. Order deterministic by id.
	sort.Slice(items, func(i, j int) bool { return items[i].desired.ID < items[j].desired.ID })

	planned := 0
	for _, it := range items {
		if planned >= r.maxPlansPerTick {
			l.Warn("max plans per tick reached; remaining items deferred to next tick", map[string]any{
				"deferred": len(items) - planned,
			})
			res.Status = "degraded"
			break
		}
		if it.kind == "unchanged" {
			continue
		}
		planned++

		newID := it.desired.ID + "-" + it.desired.Revision
		intent := model.Intent{
			ID:             newID,
			Request:        it.desired.Request,
			RequiredZone:   it.desired.RequiredZone,
			NodeSelector:   it.desired.NodeSelector,
			AffinityGroups: it.desired.Groups,
		}
		in := engine.PlanInput{ClusterID: clusterID, Intents: []model.Intent{intent}, AllowRecreate: r.allowRecreate}
		if it.kind == "replace" {
			in.Replacements = map[string]scheduler.Replacement{
				newID: {OldID: it.old.ID},
			}
		}
		out, perr := r.eng.SolveAndSave(ctx, requestID+"-"+newID, in)
		if perr != nil {
			res.Status, res.Detail = "failed", "engine: "+perr.Error()
			l.Fail("plan engine error", map[string]any{"instance": newID, "err": perr.Error()})
			_ = r.st.RecordReconcileRun(ctx, runID, requestID, res.Status, res.Detail)
			return res, perr
		}
		if !out.Success {
			res.Failures = append(res.Failures, out.PlanID)
			continue
		}
		res.Plans = append(res.Plans, out.PlanID)

		// Apply: upsert the new running instance; if replacing, evict the old
		// one only AFTER successful placement. This ordering is the rolling
		// update safety guarantee.
		pl := out.Decision.Placements[0]
		if err := r.applyPlacement(ctx, clusterID, intent, pl, it); err != nil {
			res.Status, res.Detail = "failed", "apply: "+err.Error()
			l.Fail("apply placement", map[string]any{"instance": newID, "err": err.Error()})
			_ = r.st.RecordReconcileRun(ctx, runID, requestID, res.Status, res.Detail)
			return res, err
		}
		if it.kind == "replace" {
			res.Evicted = append(res.Evicted, it.old.ID)
		}
	}

	// Remove running instances no longer desired (shrink) — applied last so a
	// failing tick earlier does not shrink capacity.
	for _, id := range removals {
		if planned >= r.maxPlansPerTick {
			break
		}
		if err := r.st.RemoveRunning(ctx, clusterID, id); err != nil {
			res.Status, res.Detail = "failed", "evict: "+err.Error()
			_ = r.st.RecordReconcileRun(ctx, runID, requestID, res.Status, res.Detail)
			return res, err
		}
		res.Evicted = append(res.Evicted, id)
	}

	switch {
	case len(res.Failures) > 0 && res.Status != "degraded":
		res.Status = "degraded"
		res.Detail = fmt.Sprintf("%d plan(s) failed constraint checks", len(res.Failures))
	case res.Status == "":
		res.Status = "ok"
	}
	l.Info("reconcile tick complete", map[string]any{
		"plans_ok": len(res.Plans), "plans_failed": len(res.Failures),
		"evicted": len(res.Evicted), "result": res.Status,
	})
	if err := r.st.RecordReconcileRun(ctx, runID, requestID, res.Status, res.Detail); err != nil {
		return res, err
	}
	return res, nil
}

// applyPlacement records the new running instance and, for replacements,
// evicts the old copy only after the new placement was committed.
func (r *Reconciler) applyPlacement(ctx context.Context, clusterID string, intent model.Intent,
	pl model.Placement, it diffItem) error {
	newRec := store.RunningRecord{
		ID:      pl.InstanceID,
		NodeID:  pl.NodeID,
		Request: intent.Request,
		Groups:  intent.AffinityGroups,
	}
	if err := r.st.UpsertRunning(ctx, clusterID, newRec); err != nil {
		return err
	}
	if it.kind == "replace" {
		if err := r.st.RemoveRunning(ctx, clusterID, it.old.ID); err != nil {
			return fmt.Errorf("evict old %s after placing %s: %w", it.old.ID, pl.InstanceID, err)
		}
	}
	return nil
}

// runAsID returns the running-instance id convention: identity-revision.
func runningID(d DesiredInstance) string { return d.ID + "-" + d.Revision }

func countKind(items []diffItem, kind string) int {
	n := 0
	for _, it := range items {
		if it.kind == kind {
			n++
		}
	}
	return n
}

// diff classifies desired entries against running records.
//
// Naming convention (documented assumption): a running instance id is
// "<base>-<revision>" where <base> is the stable desired identity and
// <revision> contains no hyphens. Classification:
//
//   - desired base exists with the SAME revision         -> unchanged;
//   - desired base exists with a DIFFERENT revision      -> replace (rolling:
//     place "<base>-<newRev>" while "<base>-<oldRev>" still runs, evict after);
//   - desired base absent                                -> add;
//   - running base absent from desired                   -> removal.
func diff(desired []DesiredInstance, running []store.RunningRecord) ([]diffItem, []string) {
	byBase := map[string]store.RunningRecord{}
	for _, rr := range running {
		byBase[identityBase(rr.ID)] = rr
	}
	var items []diffItem
	desiredBases := map[string]bool{}
	for _, d := range desired {
		desiredBases[d.ID] = true
		currentID := runningID(d)
		it := diffItem{desired: d}
		if rr, ok := byBase[d.ID]; ok {
			if rr.ID == currentID {
				it.kind = "unchanged"
			} else {
				it.kind = "replace"
				cp := rr
				it.old = &cp
			}
		} else {
			it.kind = "add"
		}
		items = append(items, it)
	}
	var removals []string
	for base := range byBase {
		if !desiredBases[base] {
			removals = append(removals, byBase[base].ID)
		}
	}
	sort.Strings(removals)
	return items, removals
}

// identityBase extracts "<base>" from running id "<base>-<revision>" by
// trimming the final hyphen segment.
func identityBase(runningID string) string {
	for i := len(runningID) - 1; i >= 0; i-- {
		if runningID[i] == '-' {
			return runningID[:i]
		}
	}
	return runningID
}

// RunPeriodic starts a background ticker until ctx is canceled.
func (r *Reconciler) RunPeriodic(ctx context.Context, clusterID string, interval time.Duration) {
	if interval <= 0 {
		return
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			reqID := engine.NewRequestID()
			if _, err := r.Tick(ctx, clusterID, reqID); err != nil {
				r.log.Fail("periodic tick error", map[string]any{"err": err.Error(), "request_id": reqID})
			}
		}
	}
}
