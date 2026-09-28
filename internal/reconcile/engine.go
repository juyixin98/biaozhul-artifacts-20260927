// Package reconcile executes plans against the provider with strict recovery
// semantics.
//
// Drift gate
//   - A never-started plan is bound to an observation digest. Apply re-observes
//     and refuses (state_conflict/DRIFT_DETECTED) on any difference.
//   - A plan interrupted mid-apply resumes in *recovery mode*: per pending step
//     the engine validates the step's concrete precondition against fresh Reads
//     rather than the whole digest, because already-succeeded writes legitimately
//     changed reality.
//
// Commit ambiguity
//   - Every write carries a deterministic idempotency key
//     "<plan>/<order>/<verb>" persisted BEFORE the call. An unknown-commit
//     fault is resolved by Read: landed -> adopt the real token (no duplicate
//     create, no mistaken delete); absent -> safe retry with the same key.
//
// Failure classification is preserved on the step row (err_cat, err_code).
package reconcile

import (
	"context"
	"encoding/json"
	"fmt"

	"infraplanner/internal/adapter"
	"infraplanner/internal/canonical"
	"infraplanner/internal/errorsx"
	"infraplanner/internal/model"
	"infraplanner/internal/observe"
	"infraplanner/internal/plan"
	"infraplanner/internal/registry"
	"infraplanner/internal/storage"
)

const maxTransientAttempts = 3

// Report is the outcome of an apply or resume.
type Report struct {
	PlanID        string `json:"plan_id"`
	Mode          string `json:"plan_mode"` // fresh | recovery
	Status        string `json:"status"`    // applied | rejected | failed
	TotalSteps    int    `json:"total_steps"`
	Succeeded     int    `json:"succeeded"`
	SkippedNoop   int    `json:"skipped_noop"`
	FailedStep    int    `json:"failed_step,omitempty"`
	ErrorCategory string `json:"error_category,omitempty"`
	ErrorCode     string `json:"error_code,omitempty"`
	Message       string `json:"message,omitempty"`
	Resumeable    bool   `json:"resumeable"`
}

// Engine drives reconciliation.
type Engine struct {
	store *storage.Store
	prov  adapter.Provider
	obs   *observe.Observer
}

func New(store *storage.Store, prov adapter.Provider) *Engine {
	return &Engine{store: store, prov: prov, obs: observe.New(prov)}
}

// Apply loads a stored plan and executes it (fresh or resume auto-detected).
func (e *Engine) Apply(ctx context.Context, planID string) (*Report, error) {
	rec, err := e.store.GetPlan(planID)
	if err != nil {
		return nil, errorsx.State("PLAN_MISSING", "no such plan", map[string]any{"plan_id": planID})
	}
	var p plan.Plan
	if err := json.Unmarshal([]byte(rec.Payload), &p); err != nil {
		return nil, errorsx.Compute("PLAN_CORRUPT", "stored plan unreadable", map[string]any{"err": err.Error()})
	}

	steps, err := e.store.ListSteps(planID)
	if err != nil {
		return nil, err
	}
	progressed := false
	for _, st := range steps {
		if st.Status == "succeeded" || st.Status == "running" || st.Status == "ambiguous" {
			progressed = true
			break
		}
	}

	if !progressed {
		// Fresh apply: exact digest gate against current reality.
		obs, err := e.obs.Snapshot(ctx, 0)
		if err != nil {
			return nil, err
		}
		curDigest := canonical.ObservationDigest(obs)
		e.evidence(storage.EvidenceRow{
			PlanID: planID, Kind: "observe",
			Message: fmt.Sprintf("pre-apply digest check: bound=%s current=%s", p.BoundDigest, curDigest),
			Detail:  storage.MustJSON(map[string]int64{"bound_run": p.Run, "current_resources": int64(len(obs.Resources))}),
		})
		if curDigest != p.BoundDigest {
			_ = e.store.SetPlanStatus(planID, string(plan.StatusRejected))
			e.evidence(storage.EvidenceRow{
				PlanID: planID, Kind: "error", Category: string(errorsx.CategoryState),
				Code: "DRIFT_DETECTED",
				Message: "reality changed between plan and apply; refusing to execute stale plan",
				Detail:  storage.MustJSON(map[string]string{"bound": p.BoundDigest, "current": curDigest}),
			})
			return &Report{PlanID: planID, Mode: "fresh", Status: "rejected",
				TotalSteps: len(p.Steps), ErrorCategory: string(errorsx.CategoryState),
				ErrorCode: "DRIFT_DETECTED",
				Message:   "observation drift detected; re-plan required"}, nil
		}
		if err := e.store.SetPlanStatus(planID, string(plan.StatusApplying)); err != nil {
			return nil, err
		}
		return e.run(ctx, &p, "fresh")
	}

	// Recovery mode: resume by truth, not by the stale digest.
	e.evidence(storage.EvidenceRow{
		PlanID: planID, Kind: "recovery",
		Message: "plan already partially applied; resuming in recovery mode using provider Read as truth",
	})
	_ = e.store.SetPlanStatus(planID, string(plan.StatusApplying))
	return e.run(ctx, &p, "recovery")
}

func (e *Engine) run(ctx context.Context, p *plan.Plan, mode string) (*Report, error) {
	rep := &Report{PlanID: p.ID, Mode: mode, Status: "applied", TotalSteps: len(p.Steps)}

	for _, st := range p.Steps {
		row, err := e.store.GetStep(p.ID, st.Order)
		if err != nil {
			return nil, err
		}
		switch row.Status {
		case "succeeded":
			rep.Succeeded++
			if st.Action == model.ActionNoop {
				rep.SkippedNoop++
			}
			continue
		}

		// ensure a journal row exists with deterministic idem key
		if row.Status == "" || row.Status == "pending" {
			verb := st.Action
			if st.Phase == "A" {
				verb = "destroy"
			}
			idem := fmt.Sprintf("%s:%d:%s", p.ID, st.Order, verb)
			_ = e.store.EnsureStep(storage.StepState{
				PlanID: p.ID, Order: st.Order, Ref: st.Ref.String(),
				Action: string(st.Action), Status: "pending", IdemKey: idem,
			})
			row, _ = e.store.GetStep(p.ID, st.Order)
		}

		e.evidence(storage.EvidenceRow{
			PlanID: p.ID, Step: st.Order, Kind: "decision",
			Message: fmt.Sprintf("step %d %s %s (%s) — %s", st.Order, st.Action, st.Ref.String(), mode, st.Reason),
			Detail: storage.MustJSON(map[string]any{
				"phase": st.Phase, "recovery": st.Recovery,
				"changed": st.ChangedAttrs, "immutable": st.ImmutableChanges,
			}),
		})

		if err := e.execStep(ctx, p, st, row); err != nil {
			xe, _ := errorsx.AsError(err)
			cat, code := string(errorsx.CategoryOf(err)), "FAILED"
			msg := err.Error()
			if xe != nil {
				code = xe.Code
			}
			// Persist classification on the step.
			row.Status = e.failureStatus(xe)
			row.Attempts++
			row.ErrCat, row.ErrCode, row.ErrMsg = cat, code, msg
			_ = e.store.UpdateStep(*row)
			_ = e.store.SetPlanStatus(p.ID, string(plan.StatusFailed))
			e.evidence(storage.EvidenceRow{
				PlanID: p.ID, Step: st.Order, Kind: "error",
				Category: cat, Code: code, Message: msg,
			})
			rep.Status = "failed"
			rep.FailedStep = st.Order
			rep.ErrorCategory, rep.ErrorCode, rep.Message = cat, code, msg
			rep.Resumeable = cat == string(errorsx.CategoryTransient) ||
				cat == string(errorsx.CategoryUnknown) ||
				cat == string(errorsx.CategoryState)
			return rep, nil
		}

		row.Status = "succeeded"
		row.ErrCat, row.ErrCode, row.ErrMsg = "", "", ""
		_ = e.store.UpdateStep(*row)
		rep.Succeeded++
		if st.Action == model.ActionNoop {
			rep.SkippedNoop++
		}
		e.evidence(storage.EvidenceRow{
			PlanID: p.ID, Step: st.Order, Kind: "result",
			Message: fmt.Sprintf("step %d %s %s succeeded", st.Order, st.Action, st.Ref.String()),
			Detail:  storage.MustJSON(map[string]string{"token": row.Token}),
		})
	}

	_ = e.store.SetPlanStatus(p.ID, string(plan.StatusApplied))
	e.evidence(storage.EvidenceRow{
		PlanID: p.ID, Kind: "result",
		Message: fmt.Sprintf("plan fully applied in %s mode (%d steps)", mode, rep.Succeeded),
	})
	return rep, nil
}

func (e *Engine) failureStatus(xe *errorsx.Error) string {
	if xe != nil && xe.Cat == errorsx.CategoryUnknown {
		return "ambiguous"
	}
	return "failed"
}

// execStep runs one step, retrying only transient faults and resolving
// unknown-commit outcomes by Read.
func (e *Engine) execStep(ctx context.Context, p *plan.Plan, st *plan.Step, row *storage.StepState) error {
	attempt := 0
	for {
		attempt++
		row.Attempts = attempt - 1
		row.Status = "running"
		_ = e.store.UpdateStep(*row)

		err := e.dispatch(ctx, st, row)
		if err == nil {
			return nil
		}
		xe, _ := errorsx.AsError(err)

		if xe != nil && xe.Cat == errorsx.CategoryUnknown {
			// Ambiguous: resolve against reality.
			resolved, rerr := e.resolveAmbiguous(ctx, st, row)
			if rerr != nil {
				return rerr
			}
			if resolved {
				return nil
			}
			// not landed: loop to retry with the same idempotency key
			if attempt >= maxTransientAttempts {
				return err
			}
			continue
		}

		if xe != nil && xe.Cat == errorsx.CategoryTransient && attempt < maxTransientAttempts {
			e.evidence(storage.EvidenceRow{
				PlanID: p.ID, Step: st.Order, Kind: "action",
				Category: string(xe.Cat), Code: xe.Code,
				Message: fmt.Sprintf("transient failure attempt %d; retrying with same idem key", attempt),
			})
			continue
		}
		return err
	}
}

// dispatch performs the step's concrete behavior.
func (e *Engine) dispatch(ctx context.Context, st *plan.Step, row *storage.StepState) error {
	live, err := e.prov.Read(ctx, st.Ref)
	if err != nil {
		return err
	}

	switch st.Action {
	case model.ActionNoop:
		if live == nil {
			return errorsx.State("PRECONDITION_VIOLATED",
				"noop target missing from reality", map[string]any{"ref": st.Ref.String()})
		}
		if !attrsEqual(live.Attrs, st.After) {
			return errorsx.State("RECOVERY_DRIFT",
				"resource changed unexpectedly during recovery",
				map[string]any{"ref": st.Ref.String(), "live": live.Attrs, "expected": st.After})
		}
		row.Token = live.ProviderToken
		return nil

	case model.ActionCreate:
		if live != nil {
			// Only acceptable if it already converges (idempotent re-entry /
			// revealed lost commit). A differently-shaped resource is drift.
			if attrsEqual(live.Attrs, st.After) {
				e.evidence(storage.EvidenceRow{
					PlanID: row.PlanID, Step: row.Order, Kind: "recovery",
					Message: "create target already exists and converges; adopting real instance (no duplicate create)",
					Detail:  storage.MustJSON(map[string]string{"token": live.ProviderToken}),
				})
				row.Token = live.ProviderToken
				return nil
			}
			return errorsx.State("RECOVERY_DRIFT",
				"create target exists with different attributes",
				map[string]any{"ref": st.Ref.String()})
		}
		token, err := e.prov.Create(ctx, model.Spec{
			Kind: st.Ref.Kind, ID: st.Ref.ID, Attrs: st.After, DependsOn: st.DependsOn,
			Protected: st.Protected,
		}, row.IdemKey)
		if err != nil {
			return err
		}
		row.Token = token
		return nil

	case model.ActionUpdate:
		if live == nil {
			return errorsx.State("PRECONDITION_VIOLATED",
				"update target missing from reality", map[string]any{"ref": st.Ref.String()})
		}
		// Drift guard: mutable-only steps require that non-target attributes did
		// not move; target attributes moving to a third value is also refused.
		if unexpectedChange(st.Before, st.After, live.Attrs) {
			return errorsx.State("RECOVERY_DRIFT",
				"resource drifted from the snapshot used to plan this update",
				map[string]any{"ref": st.Ref.String(), "live": live.Attrs})
		}
		if attrsEqual(live.Attrs, st.After) {
			row.Token = live.ProviderToken
			return nil
		}
		return e.prov.Update(ctx, st.Ref, st.After)

	case model.ActionDelete:
		if live == nil {
			row.Token = "deleted"
			return nil // already gone -> idempotent success
		}
		return e.prov.Delete(ctx, st.Ref, row.IdemKey)

	case model.ActionReplace:
		if st.Phase == "A" {
			// destroy half; tolerate the resource already being gone
			if live == nil {
				row.Token = "deleted"
				return nil
			}
			return e.prov.Delete(ctx, st.Ref, row.IdemKey)
		}
		// create half: if the new instance is already present, adopt; if old
		// instance is still present, delete then create (interrupted between A
		// and B cannot normally happen, but Read decides truth).
		if live != nil {
			if attrsEqual(live.Attrs, st.After) {
				row.Token = live.ProviderToken
				return nil
			}
			if err := e.prov.Delete(ctx, st.Ref, row.IdemKey+":cleanup"); err != nil {
				return err
			}
		}
		token, err := e.prov.Create(ctx, model.Spec{
			Kind: st.Ref.Kind, ID: st.Ref.ID, Attrs: st.After, DependsOn: st.DependsOn,
			Protected: st.Protected,
		}, row.IdemKey)
		if err != nil {
			return err
		}
		row.Token = token
		return nil
	}
	return errorsx.Compute("UNKNOWN_ACTION", "engine cannot execute action",
		map[string]any{"action": string(st.Action)})
}

// resolveAmbiguous handles a response-lost result: Read and decide.
// Returns (true,nil) if reality already satisfies the step (commit landed);
// (false,nil) if the write did not land (safe to retry); error if truth is
// still unknowable.
func (e *Engine) resolveAmbiguous(ctx context.Context, st *plan.Step, row *storage.StepState) (bool, error) {
	e.evidence(storage.EvidenceRow{
		PlanID: row.PlanID, Step: row.Order, Kind: "recovery", Category: string(errorsx.CategoryUnknown),
		Code: "RESOLVE_BY_READ",
		Message: "provider result ambiguous; reading reality to decide whether the write landed",
	})
	live, err := e.prov.Read(ctx, st.Ref)
	if err != nil {
		return false, err
	}
	satisfies := func() bool {
		switch st.Action {
		case model.ActionDelete:
			return live == nil
		case model.ActionReplace:
			if st.Phase == "A" {
				return live == nil
			}
			return live != nil && attrsEqual(live.Attrs, st.After)
		default:
			return live != nil && attrsEqual(live.Attrs, st.After)
		}
	}
	if satisfies() {
		row.Status = "ambiguous" // will be flipped to succeeded by caller
		if live != nil {
			row.Token = live.ProviderToken
		} else {
			row.Token = "deleted"
		}
		e.evidence(storage.EvidenceRow{
			PlanID: row.PlanID, Step: row.Order, Kind: "recovery",
			Message: "ambiguous write had landed; adopting real outcome without duplicate call",
			Detail:  storage.MustJSON(map[string]string{"token": row.Token}),
		})
		return true, nil
	}
	e.evidence(storage.EvidenceRow{
		PlanID: row.PlanID, Step: row.Order, Kind: "recovery",
		Message: "ambiguous write did not land; safe retry with same idempotency key",
	})
	return false, nil
}

// unexpectedChange reports whether live differs from the planned transition:
// any attribute outside the expected after-set is a third-party drift.
func unexpectedChange(before, after, live map[string]string) bool {
	for k, v := range live {
		if want, ok := after[k]; ok {
			if v != want {
				// different value; acceptable only if it is still the before
				// value (update not applied yet)
				if b, had := before[k]; had && v == b {
					continue
				}
				return true
			}
		} else {
			return true
		}
	}
	for k := range after {
		if _, ok := live[k]; !ok {
			return true
		}
	}
	return false
}

func attrsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// evidence appends to the durable evidence log, best effort.
func (e *Engine) evidence(r storage.EvidenceRow) {
	_ = e.store.AddEvidence(r)
}

// Compile-time guard that registry is referenced for attribute semantics docs.
var _ = registry.CanonicalSignature
