package reconciler

import (
	"context"
	"encoding/json"
	"fmt"

	"infraplanner/internal/journal"
	"infraplanner/internal/model"
	"infraplanner/internal/planner"
	"infraplanner/internal/provider"
)

// openLogger builds a per-run logger when a factory is configured.
func (s *Service) openLogger(runID string) (Logger, error) {
	if s.NewLogger == nil {
		return nil, nil
	}
	return s.NewLogger(runID)
}

// Apply executes a freshly planned run. Resume must be used for any run that
// already started applying (or was interrupted).
func (s *Service) Apply(ctx context.Context, runID string) (*ApplyResponse, error) {
	return s.run(ctx, runID, false)
}

// Resume continues an interrupted or failed run from the real outcome.
func (s *Service) Resume(ctx context.Context, runID string) (*ApplyResponse, error) {
	return s.run(ctx, runID, true)
}

func (s *Service) run(ctx context.Context, runID string, resume bool) (*ApplyResponse, error) {
	lg, err := s.openLogger(runID)
	if err != nil {
		return nil, model.E(model.CatCompute, "logger", "%v", err)
	}
	defer closeLogger(lg)

	run, err := s.store.GetRun(ctx, runID)
	if err != nil {
		return nil, model.E(model.CatCompute, "journal_read", "%v", err)
	}
	if run == nil {
		return nil, model.E(model.CatInput, "unknown_run", "no such run %q", runID)
	}

	ops, fp, baseline, err := decodePlan(run.PlanJSON)
	if err != nil {
		return nil, model.E(model.CatCompute, "plan_decode", "%v", err)
	}

	// State guard.
	switch run.State {
	case model.RunPlanned:
		// fresh apply allowed
	case model.RunInterrupted, model.RunFailed:
		if !resume {
			return nil, model.E(model.CatConflict, "run_not_resumable_directly",
				"run %s is %s; resume it explicitly", runID, run.State)
		}
	case model.RunApplying:
		// A crashed process leaves "applying"; resume repairs it.
		if !resume {
			return nil, model.E(model.CatConflict, "run_inflight",
				"run %s was left applying after a crash; resume it", runID)
		}
	case model.RunSucceeded:
		return s.completedResponse(runID, ops,
			model.E(model.CatConflict, "already_succeeded",
				"run %s already succeeded; refusing to re-apply against possible drift", runID))
	default:
		return nil, model.E(model.CatConflict, "bad_run_state",
			"run %s in state %s", runID, run.State)
	}

	// Re-observe and enforce the drift gate against the bound observation.
	current, err := s.prov.Observe(ctx)
	if err != nil {
		return nil, provider.EnsureError(err)
	}
	if err := s.driftGate(ctx, lg, runID, baseline, current, fp, resume); err != nil {
		me := asTyped(err)
		_ = s.store.SetRunState(context.Background(), runID, model.RunFailed, me)
		return s.completedResponse(runID, ops, me)
	}

	if err := s.store.SetRunState(ctx, runID, model.RunApplying, nil); err != nil {
		return nil, model.E(model.CatCompute, "journal_write", "%v", err)
	}
	if lg != nil {
		lg.Info("apply", "drift gate passed; applying", map[string]any{"resume": resume, "ops": len(ops)})
	}

	parsed, perr := parseSilent(run.Spec)
	if perr != nil {
		return nil, perr
	}

	resp := s.execute(ctx, lg, runID, parsed, ops, current)
	return resp, nil
}

// driftGate compares the current observation with the plan baseline.
//
// First apply: the observations must be identical.
// Resume: resources this run has already finished operating on are allowed to
// differ from the baseline (they changed because of us); any other
// difference is external drift and is refused.
func (s *Service) driftGate(ctx context.Context, lg Logger, runID string,
	baseline, current model.Observation, fp string, resume bool) error {

	curByKey := current.ByKey()
	baseByKey := baseline.ByKey()

	// On resume, exempt resources whose operation has already been started
	// (succeeded, failed or inflight). Such an op may itself have changed the
	// world during the crashed run — committed a create or performed a delete
	// — so its target differing from the baseline is our own doing, not
	// external drift. Pending (never started) resources get no exemption: an
	// unexpected change there really is an external actor.
	touched := map[model.Key]bool{}
	if resume {
		rows, err := s.store.ListOps(ctx, runID)
		if err != nil {
			return model.E(model.CatCompute, "journal_read", "%v", err)
		}
		for _, r := range rows {
			if r.State != model.OpPending {
				touched[r.Key] = true
			}
		}
	}

	for k, b := range baseByKey {
		if touched[k] {
			continue
		}
		c, ok := curByKey[k]
		if !ok {
			// A started delete is allowed to have removed its target; a
			// pending/exempt resource disappearing is external drift.
			return model.E(model.CatConflict, "drift",
				"observed drift before apply: %s existed in plan baseline but is now gone", k)
		}
		if !sameLive(b, c) {
			return model.E(model.CatConflict, "drift",
				"observed drift before apply: %s changed since planning (%s)", k, liveDiff(b, c))
		}
	}
	for k := range curByKey {
		if touched[k] {
			continue
		}
		if _, ok := baseByKey[k]; !ok {
			return model.E(model.CatConflict, "drift",
				"observed drift before apply: unexpected new resource %s", k)
		}
	}
	if !resume {
		if nowFP := planner.Fingerprint(current); nowFP != fp {
			return model.E(model.CatConflict, "drift",
				"observation fingerprint differs from bound plan")
		}
	}
	if lg != nil {
		lg.Info("drift_gate", "observation matches bound plan", map[string]any{"resume": resume})
	}
	return nil
}

// execute walks operations in journal order, resuming each from real state.
func (s *Service) execute(ctx context.Context, lg Logger, runID string,
	desired *specSpec, ops []planner.Operation, current model.Observation) *ApplyResponse {

	created := map[model.Key]model.Live{}
	baseline := current.ByKey()
	results := make([]OpResult, 0, len(ops))
	var terminal *model.Error

	for _, op := range ops {
		row, err := s.store.GetOp(ctx, runID, op.Seq)
		if err != nil {
			terminal = model.E(model.CatCompute, "journal_read", "op %d: %v", op.Seq, err)
			break
		}

		switch row.State {
		case model.OpSucceeded:
			results = append(results, opResultFromRow(row, "already complete from previous attempt"))
			// Rebuild the resolver view from the persisted physical ID so
			// later ops in this resume can reference it.
			if _, ok := created[row.Key]; !ok && op.Type != planner.OpDelete && row.PhysicalID != "" {
				created[row.Key] = model.Live{Key: row.Key, ID: row.PhysicalID}
			}
			if lg != nil {
				lg.Info("apply", "skipping succeeded op", map[string]any{"seq": op.Seq, "key": row.Key.String()})
			}
			continue
		case model.OpInflight:
			// Crash/interruption happened while the provider call was live.
			// We must never assume its outcome.
		case model.OpFailed, model.OpPending:
			// attempt (or retry)
		}

		res, fatal := s.execOne(ctx, lg, runID, desired, op, row, created, baseline)
		results = append(results, res)
		if fatal != nil {
			terminal = fatal
			break
		}
	}

	state := model.RunSucceeded
	if terminal != nil {
		if ctx.Err() != nil || isInterrupt(terminal) {
			state = model.RunInterrupted
		} else {
			state = model.RunFailed
		}
		// Persist with a detached context: even if our caller cancelled, the
		// terminal state must be durable.
		_ = s.store.SetRunState(context.Background(), runID, state, terminal)
		if lg != nil {
			lg.Error("apply", "run terminated", terminal, map[string]any{"state": string(state)})
		}
	} else {
		_ = s.store.SetRunState(context.Background(), runID, state, nil)
		if lg != nil {
			lg.Info("apply", "run succeeded", map[string]any{"ops": len(ops)})
		}
	}

	ev, _ := s.store.ListEvidence(context.Background(), runID)
	completed := 0
	for _, r := range results {
		if r.State == model.OpSucceeded {
			completed++
		}
	}
	out := &ApplyResponse{
		RunID: runID, State: state, Completed: completed, Total: len(ops),
		Results: results, Evidence: ev,
	}
	if terminal != nil {
		out.Error = typed(terminal)
	}
	return out
}

// execOne executes a single op with outcome-driven recovery semantics.
func (s *Service) execOne(ctx context.Context, lg Logger, runID string,
	desired *specSpec, op planner.Operation, row *journal.OpRow,
	created map[model.Key]model.Live, baseline map[model.Key]model.Live) (OpResult, *model.Error) {

	resolver := func(ref model.Ref) (string, error) {
		k := ref.Key()
		if l, ok := created[k]; ok && l.ID != "" {
			return l.ID, nil
		}
		if l, ok := baseline[k]; ok && l.ID != "" {
			return l.ID, nil
		}
		return "", fmt.Errorf("no physical id for %s", k)
	}

	d, _ := desired.Get(op.Key)
	var lastErr *model.Error

	for row.Attempts < s.MaxAttempts {
		// Fresh observation drives the decision; this is the "real result".
		obs, oerr := s.prov.Observe(ctx)
		if oerr != nil {
			lastErr = asTyped(oerr)
			row.Attempts++
			_ = s.persistOp(context.Background(), runID, row, lastErr)
			continue
		}
		liveByKey := obs.ByKey()

		// A create may adopt an already-present resource only when we have
		// actually attempted it before (recovery after a lost response or a
		// crash). On the first attempt a present-but-not-in-baseline resource
		// is not ours to claim: we still issue Create and let the provider
		// arbitrate (it may report exhaustion, a conflict, or idempotently
		// return an existing resource).
		attempted := row.Attempts > 0 || row.State == model.OpInflight
		decision := decide(op, row, liveByKey, baseline, attempted)
		s.evidence(ctx, lg, runID, op.Seq, row.Attempts+1, "observe",
			map[string]any{"decision": decision, "present": presentKeys(liveByKey)})

		switch decision {
		case "already_satisfied":
			row.State = model.OpSucceeded
			if l, ok := liveByKey[op.Key]; ok && op.Type != planner.OpDelete {
				row.PhysicalID = l.ID
				created[op.Key] = l
			}
			row.Err = nil
			_ = s.persistOp(context.Background(), runID, row, nil)
			return opResultFromRow(row, "verified from real observation"), nil
		case "abort_deleted":
			// A resource we meant to update/create-on-replace was deleted
			// externally; that is drift mid-apply.
			e := model.E(model.CatConflict, "vanished",
				"%s disappeared during apply", op.Key)
			_ = s.persistOp(context.Background(), runID, row, e)
			return opResultFromRow(row, "resource vanished"), e
		case "proceed":
			// fall through to issue the call
		}

		row.Attempts++
		row.State = model.OpInflight
		_ = s.persistOp(context.Background(), runID, row, nil)

		reqBody := map[string]any{"attempt": row.Attempts, "op": op.Type, "key": op.Key.String()}
		s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "request", reqBody)

		var perr error
		switch op.Type {
		case planner.OpCreate:
			cr, err := s.prov.Create(ctx, d, resolver)
			if err == nil {
				row.PhysicalID = cr.ID
				created[op.Key] = cr.Live
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "response",
					map[string]any{"id": cr.ID, "key": op.Key.String()})
			} else {
				perr = err
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "error", errMap(err))
			}
		case planner.OpUpdate:
			l, err := s.prov.Update(ctx, row.ExistingID, d, resolver)
			if err == nil {
				row.PhysicalID = l.ID
				created[op.Key] = l
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "response",
					map[string]any{"id": l.ID, "key": op.Key.String()})
			} else {
				perr = err
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "error", errMap(err))
			}
		case planner.OpDelete:
			if err := s.prov.Delete(ctx, row.ExistingID, op.Key); err != nil {
				perr = err
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "error", errMap(err))
			} else {
				delete(created, op.Key)
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "response",
					map[string]any{"deleted_id": row.ExistingID})
			}
		}

		if perr == nil {
			// Verify against the real world before declaring success. This is
			// what closes the lost-response hole: even if Create reported
			// failure, success is decided by observation on the next loop;
			// here a nominal success must also be observable.
			vobs, verr := s.prov.Observe(context.Background())
			satisfied := false
			if verr == nil {
				satisfied = opSatisfied(op, vobs.ByKey())
			}
			if satisfied {
				row.State = model.OpSucceeded
				row.Err = nil
				_ = s.persistOp(context.Background(), runID, row, nil)
				if lg != nil {
					lg.Info("apply", "op succeeded", map[string]any{
						"seq": op.Seq, "key": op.Key.String(), "attempts": row.Attempts})
				}
				return opResultFromRow(row, ""), nil
			}
			lastErr = model.E(model.CatCompute, "success_unverified",
				"%s reported success but not yet observable", op.Key)
			row.State = model.OpFailed
			_ = s.persistOp(context.Background(), runID, row, lastErr)
			// loop: next observation may confirm it (eventual consistency).
			continue
		}

		// Call returned an error. For ambiguous outcomes (a create that may
		// have committed) settle the result immediately from a fresh
		// observation instead of guessing: if the target state is already
		// present, adopt it as success; otherwise the op stays failed and can
		// be retried/resumed. This guarantees at-most-once creation.
		me := asTyped(perr)
		lastErr = me
		if me.Code == "response_lost" || me.Code == "outcome_unknown" {
			aobs, aerr := s.prov.Observe(context.Background())
			if aerr == nil && opSatisfied(op, aobs.ByKey()) {
				if l, ok := aobs.ByKey()[op.Key]; ok && op.Type != planner.OpDelete {
					row.PhysicalID = l.ID
					created[op.Key] = l
				}
				row.State = model.OpSucceeded
				row.Err = nil
				_ = s.persistOp(context.Background(), runID, row, nil)
				s.evidence(ctx, lg, runID, op.Seq, row.Attempts, "decision",
					map[string]any{"decision": "adopted_committed_after_ambiguous_response",
						"key": op.Key.String()})
				if lg != nil {
					lg.Info("apply", "ambiguous create confirmed committed; adopting",
						map[string]any{"seq": op.Seq, "key": op.Key.String()})
				}
				return opResultFromRow(row, "adopted committed resource after ambiguous response"), nil
			}
			if lg != nil {
				lg.Warn("apply", "ambiguous outcome not confirmed present",
					map[string]any{"seq": op.Seq, "key": op.Key.String(), "code": me.Code})
			}
			// outcome_unknown means the provider rolled the commit back and the
			// fresh observation confirms nothing exists: a retry is safe and
			// cannot create a duplicate. response_lost, however, means the
			// commit may exist but simply not be observable yet (replication
			// lag); issuing another create now risks a duplicate, so stop and
			// let a later resume re-observe.
			row.State = model.OpFailed
			_ = s.persistOp(context.Background(), runID, row, me)
			if me.Code == "outcome_unknown" {
				continue
			}
			return opResultFromRow(row, me.Message), me
		}
		if ctx.Err() != nil {
			row.State = model.OpInflight
			_ = s.persistOp(context.Background(), runID, row, me)
			if lg != nil {
				lg.Warn("apply", "interrupted during provider call; outcome unknown",
					map[string]any{"seq": op.Seq, "key": op.Key.String()})
			}
			ie := model.E(model.CatCompute, "interrupted",
				"interrupted during %s of %s: %v", op.Type, op.Key, me)
			return opResultFromRow(row, "interrupted; outcome to be resolved by re-observation"), ie
		}
		row.State = model.OpFailed
		_ = s.persistOp(context.Background(), runID, row, me)
		// State conflicts are non-retryable: re-running the same call against
		// the same contradictory state won't help. Exhaustion and transient
		// compute failures are retried up to MaxAttempts (capacity may free
		// up; a blip may clear).
		if me.Category == model.CatConflict {
			return opResultFromRow(row, me.Message), me
		}
		if lg != nil {
			lg.Warn("apply", "retryable failure; will retry",
				map[string]any{"seq": op.Seq, "attempt": row.Attempts,
					"category": me.Category, "code": me.Code})
		}
	}

	e := lastErr
	if e == nil {
		e = model.E(model.CatCompute, "exhausted_attempts",
			"op %d exhausted %d attempts", op.Seq, s.MaxAttempts)
	}
	return opResultFromRow(row, e.Message), e
}

// decide returns the recovery action for an op given a fresh observation.
//
//   - already_satisfied: real world already in target state; mark succeeded.
//   - proceed: issue the provider call.
//   - abort_deleted: external drift makes the op impossible/unsafe.
//
// For creates, an observed resource is adopted automatically only after a real
// attempt (attempted) or when it was already present in the plan baseline. On
// the very first attempt a brand-new, non-baseline resource is not assumed to
// be ours — proceeding lets the provider (idempotency/capacity) arbitrate.
func decide(op planner.Operation, row *journal.OpRow,
	live, baseline map[model.Key]model.Live, attempted bool) string {

	l, present := live[op.Key]
	switch op.Type {
	case planner.OpCreate:
		if present {
			_, inBaseline := baseline[op.Key]
			if attempted || inBaseline {
				return "already_satisfied"
			}
			return "proceed"
		}
		return "proceed"
	case planner.OpDelete:
		if !present {
			return "already_satisfied"
		}
		// A reassigned physical ID means the old resource was deleted and a
		// new one recreated logically: never delete the new one with a stale
		// id. That is drift, not a successful delete.
		if row.ExistingID != "" && l.ID != row.ExistingID {
			return "abort_deleted"
		}
		return "proceed"
	case planner.OpUpdate:
		if !present {
			return "abort_deleted"
		}
		if l.ID != row.ExistingID {
			return "abort_deleted"
		}
		return "proceed"
	}
	return "proceed"
}

func opSatisfied(op planner.Operation, live map[model.Key]model.Live) bool {
	_, present := live[op.Key]
	switch op.Type {
	case planner.OpCreate, planner.OpUpdate:
		return present
	case planner.OpDelete:
		return !present
	}
	return false
}

// persistOp writes an op row using the supplied (possibly detached) context.
func (s *Service) persistOp(ctx context.Context, runID string, row *journal.OpRow, opErr *model.Error) error {
	row.Err = opErr
	row.UpdatedAt = timeNow()
	return s.store.UpsertOp(ctx, runID, *row)
}

func (s *Service) evidence(ctx context.Context, lg Logger, runID string,
	seq, attempt int, kind string, body map[string]any) {
	if body == nil {
		body = map[string]any{}
	}
	b, _ := json.Marshal(body)
	e := journal.Evidence{
		RunID: runID, Seq: seq, Attempt: attempt, Kind: kind, Body: string(b),
	}
	_ = s.store.AddEvidence(context.Background(), e)
	if lg != nil {
		lg.Info("evidence", kind, map[string]any{"seq": seq, "attempt": attempt, "body": string(b)})
	}
}

func (s *Service) completedResponse(runID string, ops []planner.Operation, e *model.Error) (*ApplyResponse, error) {
	if e != nil {
		return &ApplyResponse{
			RunID: runID, State: model.RunFailed, Total: len(ops),
			Error: typed(e),
		}, nil
	}
	return nil, nil
}

// ---- small helpers ----

func typed(e *model.Error) *TypedError {
	if e == nil {
		return nil
	}
	return &TypedError{Category: e.Category, Code: e.Code, Message: e.Message}
}

func asTyped(err error) *model.Error {
	if me, ok := model.AsError(err); ok {
		return me
	}
	return model.E(model.CatCompute, "provider_error", "%v", err)
}

func isInterrupt(e *model.Error) bool {
	return e.Code == "interrupted" || e.Code == "cancelled"
}

func opResultFromRow(row *journal.OpRow, reason string) OpResult {
	return OpResult{
		Seq: row.Seq, Type: planner.OpType(row.Type), Key: row.Key,
		State: row.State, PhysicalID: row.PhysicalID, Attempts: row.Attempts,
		Reason: reason, Error: typed(row.Err),
	}
}

func presentKeys(m map[model.Key]model.Live) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k.String())
	}
	return out
}

func errMap(err error) map[string]any {
	me := asTyped(err)
	return map[string]any{"category": me.Category, "code": me.Code, "message": me.Message}
}
