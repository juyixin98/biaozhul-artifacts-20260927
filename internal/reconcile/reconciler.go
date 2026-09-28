// Package reconcile contains the reconciliation loop: the bridge between the
// desired state persisted by the API and the actual resource service reached
// through ExternalClient.
//
// Invariants enforced here:
//
//   - observedGeneration moves forward when the controller has made a decision
//     about that generation; reconciledGeneration moves forward only when the
//     external service is *confirmed* to hold that spec. An ambiguous or stale
//     observation never completes a generation.
//   - Resources under deletion keep their record until the external object is
//     gone and the deletion finalizer has been removed.
//   - A create whose response is lost is claimed by deterministic id/GET; the
//     controller never issues a second create for the same desired object.
//   - A conflict (local resource_version or external version) causes a
//     requeue-and-reload, never an overwrite of newer desired state.
package reconcile

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"

	"resourcecontroller/internal/diag"
	"resourcecontroller/internal/model"
	"resourcecontroller/internal/store"
)

// FinalizerExternalCleanup marks a resource whose external object must be
// deleted before the local record may go away.
const FinalizerExternalCleanup = "resourcecontroller.dev/external-cleanup"

// Config holds loop timing knobs.
type Config struct {
	Workers        int
	ResyncInterval time.Duration
	BackoffBase    time.Duration
	BackoffMax     time.Duration
}

// Enqueuer is anything the API can nudge when desired state changes.
type Enqueuer interface {
	Enqueue(name string)
}

// Reconciler runs the control loop.
type Reconciler struct {
	st   *store.Store
	ext  ExternalClient
	log  *diag.Logger
	cfg  Config
	q    *queue
	stop context.CancelFunc
	done chan struct{}

	mu       chan struct{} // serializes access to attempts map (1-slot semaphore)
	attempts map[string]int
}

// New builds a reconciler.
func New(st *store.Store, ext ExternalClient, log *diag.Logger, cfg Config) *Reconciler {
	if cfg.Workers <= 0 {
		cfg.Workers = 2
	}
	if cfg.ResyncInterval <= 0 {
		cfg.ResyncInterval = 30 * time.Second
	}
	if cfg.BackoffBase <= 0 {
		cfg.BackoffBase = 200 * time.Millisecond
	}
	if cfg.BackoffMax <= 0 {
		cfg.BackoffMax = 30 * time.Second
	}
	return &Reconciler{
		st:       st,
		ext:      ext,
		log:      log,
		cfg:      cfg,
		q:        newQueue(),
		attempts: map[string]int{},
		mu:       make(chan struct{}, 1),
	}
}

// Enqueue implements Enqueuer; duplicate adds collapse in the queue.
func (r *Reconciler) Enqueue(name string) { r.q.Add(name) }

// Run starts workers and the periodic resync until ctx is canceled.
func (r *Reconciler) Run(ctx context.Context) {
	ctx, cancel := context.WithCancel(ctx)
	r.stop = cancel
	r.done = make(chan struct{})

	for i := 0; i < r.cfg.Workers; i++ {
		go r.worker(ctx, i)
	}
	go r.resyncLoop(ctx)

	<-ctx.Done()
	r.q.Shutdown()
	close(r.done)
}

// Stop asks the loop to stop and waits for it to wind down.
func (r *Reconciler) Stop() {
	if r.stop != nil {
		r.stop()
	}
	if r.done != nil {
		<-r.done
	}
}

func (r *Reconciler) resyncLoop(ctx context.Context) {
	t := time.NewTicker(r.cfg.ResyncInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			pending, err := r.st.ListPending(ctx)
			if err != nil {
				r.log.Error(ctx, "resync list failed", "error", err.Error())
				continue
			}
			for _, w := range pending {
				r.q.Add(w.Meta.Name)
			}
		}
	}
}

func (r *Reconciler) worker(ctx context.Context, id int) {
	for {
		key, ok := r.q.Get()
		if !ok {
			return
		}
		retry := r.handle(ctx, key)
		r.q.Done(key)
		if retry.after > 0 {
			r.q.AddAfter(key, retry.after)
		}
	}
}

type retryDecision struct {
	after time.Duration
}

// noRetry completes the current pass without scheduling a delayed retry
// (either done, or requeued immediately inside handle).
var noRetry = retryDecision{}

func (r *Reconciler) backoff(name string) time.Duration {
	r.mu <- struct{}{}
	n := r.attempts[name]
	r.attempts[name] = n + 1
	<-r.mu
	d := r.cfg.BackoffBase
	for i := 0; i < n && d < r.cfg.BackoffMax; i++ {
		d *= 2
		if d > r.cfg.BackoffMax {
			d = r.cfg.BackoffMax
		}
	}
	return d
}

func (r *Reconciler) resetBackoff(name string) {
	r.mu <- struct{}{}
	delete(r.attempts, name)
	<-r.mu
}

func (r *Reconciler) handle(ctx context.Context, name string) retryDecision {
	w, err := r.st.Get(ctx, name)
	if err != nil {
		if err == store.ErrNotFound {
			r.resetBackoff(name)
			return noRetry
		}
		r.log.Error(ctx, "reconcile load failed", "resource", name, "error", err.Error())
		return retryDecision{after: r.backoff(name)}
	}

	if w.Meta.DeletionTimestamp != nil {
		return r.reconcileDelete(ctx, w)
	}
	return r.reconcileDesired(ctx, w)
}

// ---------------------------------------------------------------------------
// Desired-state path
// ---------------------------------------------------------------------------

func (r *Reconciler) reconcileDesired(ctx context.Context, w *model.Widget) retryDecision {
	rid := diag.NewRequestID()
	ctx = diag.WithRequestID(ctx, rid)
	gen := w.Meta.Generation

	// 1. Ensure the deletion-protection finalizer is present before any
	// external resource can exist.
	if !hasFinalizer(w, FinalizerExternalCleanup) {
		updated, err := r.st.AddFinalizer(ctx, w.Meta.Name, FinalizerExternalCleanup, w.Meta.ResourceVersion)
		if err != nil {
			if err == store.ErrVersionConflict {
				r.log.Decision(ctx, diag.DecisionArgs{
					Resource: w.Meta.Name, Phase: "observe",
					Decision: diag.DecisionRejected, Action: "requeue",
					Reason: "local-version-conflict", Category: store.DescribeError(err),
					Generation: gen, ObservedGeneration: w.Status.ObservedGeneration,
					ReconciledGeneration: w.Status.ReconciledGeneration, RequestID: rid,
				})
				r.q.Add(w.Meta.Name)
				return noRetry
			}
			r.log.Error(ctx, "add finalizer failed", "resource", w.Meta.Name, "error", err.Error())
			return retryDecision{after: r.backoff(w.Meta.Name)}
		}
		r.log.Decision(ctx, diag.DecisionArgs{
			Resource: w.Meta.Name, Phase: "observe", Decision: diag.DecisionAccepted,
			Action: "add-finalizer", Reason: "ensure-delete-protection",
			Generation: gen, RequestID: rid,
			ObservedGeneration:   updated.Status.ObservedGeneration,
			ReconciledGeneration: updated.Status.ReconciledGeneration,
		})
		r.q.Add(w.Meta.Name)
		return noRetry
	}

	extID := w.Status.ExternalID
	if extID == "" {
		extID = ExternalID(w.Meta.Name)
	}

	// 2. Observe the actual resource service.
	observed, oerr := r.ext.Observe(ctx, extID)
	switch {
	case isNotFound(oerr):
		return r.createExternal(ctx, w, extID, rid, nil)
	case oerr != nil:
		// The observation failed: no claim about this generation can be made.
		// observed/reconciled generations stay where they are.
		ce, _ := AsCallError(oerr)
		return r.recordFailure(ctx, w, "observe", ce, rid, extID, "actual-state-unknown")
	}

	desiredFP := model.SpecFingerprint(w.Spec)
	if observed.SpecFP == "" {
		observed.SpecFP = model.SpecFingerprint(specFromExternal(observed.Spec))
	}

	// 3. The observed object already matches: confirm with a second read so a
	// single stale response cannot complete the generation (see staleGet
	// fault tests), then mark in sync.
	if observed.SpecFP == desiredFP {
		confirmed, cerr := r.ext.Observe(ctx, extID)
		if cerr != nil || confirmed == nil {
			ce, _ := AsCallError(cerr)
			return r.recordFailure(ctx, w, "observe", ce, rid, extID, "confirm-read-failed")
		}
		if confirmed.SpecFP == "" {
			confirmed.SpecFP = model.SpecFingerprint(specFromExternal(confirmed.Spec))
		}
		if confirmed.SpecFP != desiredFP {
			// The two reads disagreed: the first one was stale. Refuse to
			// complete and requeue.
			r.log.Decision(ctx, diag.DecisionArgs{
				Resource: w.Meta.Name, Phase: "observe", ExternalID: extID,
				Decision: diag.DecisionRejected, Action: "requeue",
				Reason: "stale-observation", Detail: "first read matched desired, confirm read did not",
				Generation: gen, RequestID: rid, ExternalVersion: confirmed.Version,
				ObservedGeneration:   gen,
				ReconciledGeneration: w.Status.ReconciledGeneration,
			})
			return r.saveAttemptAndRequeue(ctx, w, extID, attemptRecord("observe",
				diag.DecisionRejected, "requeue", "stale-observation",
				CategoryConflict, rid, extID, confirmed.Version), gen)
		}
		return r.markInSync(ctx, w, extID, confirmed, rid)
	}

	// 4. Drift or desired generation ahead: update with optimistic concurrency.
	return r.updateExternal(ctx, w, extID, observed, rid)
}

// createExternal issues the external create. prior is non-nil when we reached
// here after an ambiguous earlier attempt and already know the object state.
func (r *Reconciler) createExternal(ctx context.Context, w *model.Widget, extID, rid string, prior *ExternalResource) retryDecision {
	spec := specToExternal(w)
	key := IdempotencyKey(w.Meta.Name, w.Meta.Generation)

	// The create response itself is not trusted for convergence; the
	// confirmation read below is the evidence we persist.
	_, err := r.ext.Create(ctx, extID, key, spec)
	switch {
	case err == nil:
		// Defensive confirmation: act on the post-create read, not just on the
		// create response.
		confirmed, cerr := r.ext.Observe(ctx, extID)
		if cerr != nil || confirmed == nil {
			ce, _ := AsCallError(cerr)
			return r.recordFailure(ctx, w, "create", ce, rid, extID, "post-create-confirm-failed")
		}
		r.resetBackoff(w.Meta.Name)
		return r.applyObserved(ctx, w, extID, confirmed, rid, "create", "created")
	case isAmbiguous(err):
		// Outcome unknown. Do NOT create again. Requeue; the next pass GETs
		// and either claims the object or finds 404 and creates.
		ce, _ := AsCallError(err)
		r.log.Decision(ctx, diag.DecisionArgs{
			Resource: w.Meta.Name, Phase: "create", ExternalID: extID,
			Decision: diag.DecisionUndecidable, Action: "claim-by-observe-next-pass",
			Reason: ce.Category, Detail: "response lost after create; will not duplicate",
			Category: ce.Code, Generation: w.Meta.Generation, RequestID: rid,
			ObservedGeneration:   w.Meta.Generation,
			ReconciledGeneration: w.Status.ReconciledGeneration,
		})
		return r.saveAttemptAndRequeue(ctx, w, extID, attemptRecord("create",
			diag.DecisionUndecidable, "claim-by-observe", "create-response-lost",
			ce.Category, rid, extID, 0), w.Meta.Generation)
	case isConflict(err):
		// Someone (a prior ambiguous attempt, or the service deduping) owns the
		// id. Reload via observe; never overwrite.
		ce, _ := AsCallError(err)
		r.log.Decision(ctx, diag.DecisionArgs{
			Resource: w.Meta.Name, Phase: "create", ExternalID: extID,
			Decision: diag.DecisionRejected, Action: "claim-by-observe",
			Reason: "external-already-exists", Category: ce.Code,
			Detail:     "create reported conflict; reloading actual state",
			Generation: w.Meta.Generation, RequestID: rid,
			ObservedGeneration:   w.Meta.Generation,
			ReconciledGeneration: w.Status.ReconciledGeneration,
		})
		r.q.Add(w.Meta.Name)
		return noRetry
	case isTransient(err), isRejected(err), isUnknown(err):
		ce, _ := AsCallError(err)
		return r.recordFailure(ctx, w, "create", ce, rid, extID, "external-create-unavailable")
	default:
		ce, _ := AsCallError(err)
		return r.recordFailure(ctx, w, "create", ce, rid, extID, "unmapped-create-error")
	}
}

func (r *Reconciler) updateExternal(ctx context.Context, w *model.Widget, extID string, observed *ExternalResource, rid string) retryDecision {
	spec := specToExternal(w)
	res, err := r.ext.Update(ctx, extID, observed.Version, spec)
	switch {
	case err == nil:
		_ = res
		// Confirm against the actual service before completing the generation.
		confirmed, cerr := r.ext.Observe(ctx, extID)
		if cerr != nil || confirmed == nil {
			ce, _ := AsCallError(cerr)
			return r.recordFailure(ctx, w, "update", ce, rid, extID, "post-update-confirm-failed")
		}
		if confirmed.SpecFP == "" {
			confirmed.SpecFP = model.SpecFingerprint(specFromExternal(confirmed.Spec))
		}
		desiredFP := model.SpecFingerprint(w.Spec)
		if confirmed.SpecFP != desiredFP {
			r.log.Decision(ctx, diag.DecisionArgs{
				Resource: w.Meta.Name, Phase: "update", ExternalID: extID,
				Decision: diag.DecisionUndecidable, Action: "requeue",
				Reason:     "post-update-mismatch",
				Detail:     "service accepted update but observation does not show desired spec yet",
				Generation: w.Meta.Generation, RequestID: rid, ExternalVersion: confirmed.Version,
				ObservedGeneration:   w.Meta.Generation,
				ReconciledGeneration: w.Status.ReconciledGeneration,
			})
			return r.saveAttemptAndRequeue(ctx, w, extID, attemptRecord("update",
				diag.DecisionUndecidable, "requeue", "post-update-mismatch",
				CategoryAmbiguous, rid, extID, confirmed.Version), w.Meta.Generation)
		}
		r.resetBackoff(w.Meta.Name)
		return r.applyObserved(ctx, w, extID, confirmed, rid, "update", "updated-to-desired")
	case isConflict(err):
		// External version moved under us: newer actual state exists. Refuse to
		// overwrite, reload on the next pass.
		ce, _ := AsCallError(err)
		r.log.Decision(ctx, diag.DecisionArgs{
			Resource: w.Meta.Name, Phase: "update", ExternalID: extID,
			Decision: diag.DecisionRejected, Action: "requeue-reload",
			Reason: "external-version-conflict", Category: ce.Code,
			Detail:     fmt.Sprintf("If-Match %d was stale; not overwriting newer spec", observed.Version),
			Generation: w.Meta.Generation, RequestID: rid, ExternalVersion: observed.Version,
			ObservedGeneration:   w.Meta.Generation,
			ReconciledGeneration: w.Status.ReconciledGeneration,
		})
		return r.saveAttemptAndRequeue(ctx, w, extID, attemptRecord("update",
			diag.DecisionRejected, "requeue-reload", "external-version-conflict",
			CategoryConflict, rid, extID, observed.Version), w.Meta.Generation)
	case isAmbiguous(err), isTransient(err), isUnknown(err), isRejected(err):
		ce, _ := AsCallError(err)
		return r.recordFailure(ctx, w, "update", ce, rid, extID, "update-inconclusive")
	default:
		ce, _ := AsCallError(err)
		return r.recordFailure(ctx, w, "update", ce, rid, extID, "unmapped-update-error")
	}
}

// markInSync records convergence when the actual resource is confirmed equal
// to the desired spec.
func (r *Reconciler) markInSync(ctx context.Context, w *model.Widget, extID string, ext *ExternalResource, rid string) retryDecision {
	r.resetBackoff(w.Meta.Name)
	return r.applyObserved(ctx, w, extID, ext, rid, "observe", "already-in-sync")
}

// applyObserved persists a confirmed external observation. reconciledGen is
// advanced to the desired generation only on this confirmed path.
func (r *Reconciler) applyObserved(ctx context.Context, w *model.Widget, extID string, ext *ExternalResource, rid, phase, action string) retryDecision {
	desiredFP := model.SpecFingerprint(w.Spec)
	matched := ext.SpecFP == desiredFP

	st := w.Status
	st.ExternalID = extID
	st.ExternalVersion = ext.Version
	st.ObservedGeneration = w.Meta.Generation
	if matched {
		st.ReconciledGeneration = w.Meta.Generation
		st.Phase = model.PhaseReady
		st.Conditions = upsertCondition(st.Conditions, model.Condition{
			Type: "Ready", Status: "True", Reason: "InSync",
			Message:            "external resource confirmed at desired spec",
			ObservedGeneration: w.Meta.Generation,
		})
	} else {
		st.Phase = model.PhaseSyncing
		st.Conditions = upsertCondition(st.Conditions, model.Condition{
			Type: "Ready", Status: "False", Reason: "SpecDrift",
			Message:            "external resource exists but does not yet match desired spec",
			ObservedGeneration: w.Meta.Generation,
		})
	}
	st.LastAttempt = attemptRecord(phase, diag.DecisionAccepted, action, "confirmed",
		"", rid, extID, ext.Version)

	saved, err := r.st.SaveStatus(ctx, w.Meta.Name, st, w.Meta.ResourceVersion)
	if err != nil {
		if err == store.ErrVersionConflict {
			r.log.Decision(ctx, diag.DecisionArgs{
				Resource: w.Meta.Name, Phase: phase, ExternalID: extID,
				Decision: diag.DecisionRejected, Action: "requeue",
				Reason: "local-version-conflict", Generation: w.Meta.Generation, RequestID: rid,
				ObservedGeneration:   w.Meta.Generation,
				ReconciledGeneration: w.Status.ReconciledGeneration,
			})
			r.q.Add(w.Meta.Name)
			return noRetry
		}
		r.log.Error(ctx, "save status failed", "resource", w.Meta.Name, "error", err.Error())
		return retryDecision{after: r.backoff(w.Meta.Name)}
	}
	r.log.Decision(ctx, diag.DecisionArgs{
		Resource: w.Meta.Name, Phase: phase, ExternalID: extID,
		Decision: diag.DecisionAccepted, Action: action,
		Reason:     map[bool]string{true: "in-sync", false: "spec-drift"}[matched],
		Generation: w.Meta.Generation, RequestID: rid, ExternalVersion: ext.Version,
		ObservedGeneration:   saved.Status.ObservedGeneration,
		ReconciledGeneration: saved.Status.ReconciledGeneration,
	})
	if matched {
		return noRetry
	}
	r.q.Add(w.Meta.Name)
	return noRetry
}

// recordFailure persists an undecidable/rejected decision that did not change
// the actual state, then returns the backoff decision.
func (r *Reconciler) recordFailure(ctx context.Context, w *model.Widget, phase string, ce *CallError, rid, extID, detail string) retryDecision {
	cat, code := CategoryUnknown, "unknown"
	if ce != nil {
		cat, code = ce.Category, ce.Code
	}
	decision := diag.DecisionUndecidable
	if cat == CategoryConflict || cat == CategoryRejected {
		decision = diag.DecisionRejected
	}
	r.log.Decision(ctx, diag.DecisionArgs{
		Resource: w.Meta.Name, Phase: phase, ExternalID: extID,
		Decision: decision, Action: "requeue-with-backoff",
		Reason: cat, Category: code, Detail: detail,
		Generation: w.Meta.Generation, RequestID: rid,
		ObservedGeneration:   w.Status.ObservedGeneration,
		ReconciledGeneration: w.Status.ReconciledGeneration,
	})
	return r.saveAttemptAndRequeue(ctx, w, extID, attemptRecord(phase, decision,
		"requeue-with-backoff", cat, code, rid, extID, w.Status.ExternalVersion), w.Status.ObservedGeneration)
}

// saveAttemptAndRequeue stores an attempt without claiming convergence and
// schedules the next pass. Observed generation is passed explicitly: on
// inconclusive observations it stays at the previously observed generation.
func (r *Reconciler) saveAttemptAndRequeue(ctx context.Context, w *model.Widget, extID string, att *model.Attempt, observedGen int64) retryDecision {
	st := w.Status
	st.ExternalID = extID
	st.ObservedGeneration = observedGen
	// reconciledGeneration intentionally untouched.
	if w.Meta.DeletionTimestamp == nil && st.ReconciledGeneration < w.Meta.Generation {
		st.Phase = model.PhaseSyncing
	}
	st.LastAttempt = att
	if att.Decision == diag.DecisionUndecidable {
		st.Conditions = upsertCondition(st.Conditions, model.Condition{
			Type: "Ready", Status: "False", Reason: "ReconcileInconclusive",
			Message:            att.Reason + ": " + att.Category,
			ObservedGeneration: w.Meta.Generation,
		})
	}
	if _, err := r.st.SaveStatus(ctx, w.Meta.Name, st, w.Meta.ResourceVersion); err != nil {
		if err == store.ErrVersionConflict {
			r.q.Add(w.Meta.Name)
			return noRetry
		}
		r.log.Error(ctx, "save attempt failed", "resource", w.Meta.Name, "error", err.Error())
	}
	return retryDecision{after: r.backoff(w.Meta.Name)}
}

// ---------------------------------------------------------------------------
// Deletion path
// ---------------------------------------------------------------------------

func (r *Reconciler) reconcileDelete(ctx context.Context, w *model.Widget) retryDecision {
	rid := diag.NewRequestID()
	ctx = diag.WithRequestID(ctx, rid)
	extID := w.Status.ExternalID
	if extID == "" {
		extID = ExternalID(w.Meta.Name)
	}

	// Phase C: finalizer already removed. The external object had better be
	// gone; confirm once more and then drop the local record.
	if !hasFinalizer(w, FinalizerExternalCleanup) {
		observed, err := r.ext.Observe(ctx, extID)
		switch {
		case isNotFound(err):
			if derr := r.st.Delete(ctx, w.Meta.Name, w.Meta.ResourceVersion); derr != nil {
				if derr == store.ErrVersionConflict {
					r.q.Add(w.Meta.Name)
					return noRetry
				}
				r.log.Error(ctx, "record delete failed", "resource", w.Meta.Name, "error", derr.Error())
				return retryDecision{after: r.backoff(w.Meta.Name)}
			}
			r.resetBackoff(w.Meta.Name)
			r.log.Decision(ctx, diag.DecisionArgs{
				Resource: w.Meta.Name, Phase: "delete", ExternalID: extID,
				Decision: diag.DecisionAccepted, Action: "remove-record",
				Reason: "external-cleanup-confirmed", Generation: w.Meta.Generation,
				RequestID: rid,
			})
			return noRetry
		case err != nil:
			ce, _ := AsCallError(err)
			return r.recordDeleteFailure(ctx, w, "observe", ce, rid, extID, "cleanup-confirm-unavailable")
		default:
			// Object reappeared (e.g. external owner recreated it): re-arm the
			// finalizer rather than deleting the record while it still has
			// backing state.
			_ = observed
			rearmed, aerr := r.st.AddFinalizer(ctx, w.Meta.Name, FinalizerExternalCleanup, w.Meta.ResourceVersion)
			if aerr == nil {
				r.log.Decision(ctx, diag.DecisionArgs{
					Resource: w.Meta.Name, Phase: "delete", ExternalID: extID,
					Decision: diag.DecisionRejected, Action: "rearm-finalizer",
					Reason:     "external-resource-present",
					Detail:     "refusing to remove record while actual resource exists",
					Generation: w.Meta.Generation, RequestID: rid,
					ObservedGeneration: rearmed.Status.ObservedGeneration,
				})
				r.q.Add(w.Meta.Name)
			}
			return noRetry
		}
	}

	// Phases A/B with finalizer present: discover the object if we do not know
	// its id status, then delete externally; once 404, remove the finalizer.
	observed, err := r.ext.Observe(ctx, extID)
	switch {
	case isNotFound(err):
		// Cleanup already complete (e.g. crash after delete before finalizer
		// removal): persist finalizer removal; the record goes on the next pass.
		return r.removeFinalizer(ctx, w, extID, rid, "external-already-gone")
	case err != nil:
		ce, _ := AsCallError(err)
		return r.recordDeleteFailure(ctx, w, "observe", ce, rid, extID, "cleanup-state-unknown")
	}

	// External object present: remember the claimed id if this is the first
	// deletion pass, then call delete.
	if w.Status.ExternalID == "" {
		st := w.Status
		st.ExternalID = extID
		st.ExternalVersion = observed.Version
		if _, serr := r.st.SaveStatus(ctx, w.Meta.Name, st, w.Meta.ResourceVersion); serr != nil {
			if serr == store.ErrVersionConflict {
				r.q.Add(w.Meta.Name)
				return noRetry
			}
			return retryDecision{after: r.backoff(w.Meta.Name)}
		}
		r.q.Add(w.Meta.Name)
		return noRetry
	}

	if derr := r.ext.Delete(ctx, extID); derr != nil {
		ce, _ := AsCallError(derr)
		if isNotFound(derr) {
			return r.removeFinalizer(ctx, w, extID, rid, "external-gone-on-delete")
		}
		return r.recordDeleteFailure(ctx, w, "delete", ce, rid, extID, "external-delete-failed")
	}

	r.resetBackoff(w.Meta.Name)
	st := w.Status
	st.Phase = model.PhaseDeleting
	st.LastAttempt = attemptRecord("delete", diag.DecisionAccepted, "external-deleted",
		"cleanup-in-progress", "", rid, extID, observed.Version)
	st.Conditions = upsertCondition(st.Conditions, model.Condition{
		Type: "DeleteFailed", Status: "False", Reason: "DeleteSucceeded",
		Message:            "external resource deleted; finalizer removal pending",
		ObservedGeneration: w.Meta.Generation,
	})
	if _, serr := r.st.SaveStatus(ctx, w.Meta.Name, st, w.Meta.ResourceVersion); serr != nil {
		if serr == store.ErrVersionConflict {
			r.q.Add(w.Meta.Name)
			return noRetry
		}
		r.log.Error(ctx, "save delete status failed", "resource", w.Meta.Name, "error", serr.Error())
		return retryDecision{after: r.backoff(w.Meta.Name)}
	}
	r.log.Decision(ctx, diag.DecisionArgs{
		Resource: w.Meta.Name, Phase: "delete", ExternalID: extID,
		Decision: diag.DecisionAccepted, Action: "external-deleted",
		Reason: "cleanup-in-progress", Generation: w.Meta.Generation, RequestID: rid,
		ExternalVersion: observed.Version,
	})
	// Next pass observes 404 and removes the finalizer; the pass after that
	// removes the record. Explicit staging makes ownership at each phase
	// testable.
	r.q.Add(w.Meta.Name)
	return noRetry
}

// removeFinalizer persists finalizer removal after confirmed external cleanup.
// The record itself is removed on the following pass (finalizer-free, 404).
func (r *Reconciler) removeFinalizer(ctx context.Context, w *model.Widget, extID, rid, reason string) retryDecision {
	updated, err := r.st.SaveSpec(ctx, cloneWithoutFinalizer(w, FinalizerExternalCleanup), w.Meta.ResourceVersion)
	if err != nil {
		if err == store.ErrVersionConflict {
			r.q.Add(w.Meta.Name)
			return noRetry
		}
		r.log.Error(ctx, "remove finalizer failed", "resource", w.Meta.Name, "error", err.Error())
		return retryDecision{after: r.backoff(w.Meta.Name)}
	}
	r.log.Decision(ctx, diag.DecisionArgs{
		Resource: w.Meta.Name, Phase: "delete", ExternalID: extID,
		Decision: diag.DecisionAccepted, Action: "remove-finalizer",
		Reason: reason, Generation: w.Meta.Generation, RequestID: rid,
		ObservedGeneration: updated.Status.ObservedGeneration,
	})
	r.q.Add(w.Meta.Name)
	return noRetry
}

func (r *Reconciler) recordDeleteFailure(ctx context.Context, w *model.Widget, phase string, ce *CallError, rid, extID, detail string) retryDecision {
	cat, code := CategoryUnknown, "unknown"
	if ce != nil {
		cat, code = ce.Category, ce.Code
	}
	r.log.Decision(ctx, diag.DecisionArgs{
		Resource: w.Meta.Name, Phase: phase, ExternalID: extID,
		Decision: diag.DecisionUndecidable, Action: "retry-delete",
		Reason: cat, Category: code, Detail: detail,
		Generation: w.Meta.Generation, RequestID: rid,
		ObservedGeneration:   w.Status.ObservedGeneration,
		ReconciledGeneration: w.Status.ReconciledGeneration,
	})
	st := w.Status
	st.Phase = model.PhaseDeleting
	st.ExternalID = extID
	st.LastAttempt = attemptRecord(phase, diag.DecisionUndecidable, "retry-delete",
		cat, code, rid, extID, st.ExternalVersion)
	st.Conditions = upsertCondition(st.Conditions, model.Condition{
		Type: "DeleteFailed", Status: "True", Reason: code,
		Message: detail, ObservedGeneration: w.Meta.Generation,
	})
	if _, err := r.st.SaveStatus(ctx, w.Meta.Name, st, w.Meta.ResourceVersion); err != nil {
		if err == store.ErrVersionConflict {
			r.q.Add(w.Meta.Name)
			return noRetry
		}
		r.log.Error(ctx, "save delete failure failed", "resource", w.Meta.Name, "error", err.Error())
	}
	return retryDecision{after: r.backoff(w.Meta.Name)}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func hasFinalizer(w *model.Widget, name string) bool {
	for _, f := range w.Meta.Finalizers {
		if f == name {
			return true
		}
	}
	return false
}

func cloneWithoutFinalizer(w *model.Widget, name string) *model.Widget {
	cp := *w
	cp.Meta.Finalizers = nil
	for _, f := range w.Meta.Finalizers {
		if f != name {
			cp.Meta.Finalizers = append(cp.Meta.Finalizers, f)
		}
	}
	return &cp
}

func specToExternal(w *model.Widget) ExternalSpec {
	return ExternalSpec{
		Name:        w.Meta.Name,
		Replicas:    w.Spec.Replicas,
		Color:       w.Spec.Color,
		SecretToken: w.Spec.SecretToken,
	}
}

func specFromExternal(s ExternalSpec) model.WidgetSpec {
	return model.WidgetSpec{Replicas: s.Replicas, Color: s.Color, SecretToken: s.SecretToken}
}

func attemptRecord(phase, decision, action, reason, category, rid, extID string, extVersion int64) *model.Attempt {
	return &model.Attempt{
		Phase: phase, Decision: decision, Action: action, Reason: reason,
		Category: category, RequestID: rid, ExternalID: extID,
		At: time.Now().UTC(),
	}
}

// upsertCondition sets the condition of the given type, updating LastTransitionTime
// only when the status changes.
func upsertCondition(prev []model.Condition, c model.Condition) []model.Condition {
	now := time.Now().UTC()
	for i, existing := range prev {
		if existing.Type != c.Type {
			continue
		}
		c.LastTransitionTime = existing.LastTransitionTime
		if existing.Status != c.Status {
			c.LastTransitionTime = now
		}
		prev[i] = c
		return prev
	}
	c.LastTransitionTime = now
	return append(prev, c)
}

// ExternalID derives the stable external object id from the resource name.
// Determinism is what makes "create then GET to claim" safe: retries always
// look for the same physical object.
func ExternalID(name string) string {
	sum := sha256.Sum256([]byte("widget:" + name))
	return "w-" + hex.EncodeToString(sum[:12])
}

// IdempotencyKey derives the create idempotency key for a generation. A new
// generation uses a new key; retries of the same generation reuse it.
func IdempotencyKey(name string, generation int64) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("widget:%s:%d", name, generation)))
	return "idem-" + hex.EncodeToString(sum[:12])
}

// error classification helpers
func isNotFound(err error) bool {
	ce, ok := AsCallError(err)
	return ok && ce.Category == CategoryNotFound
}
func isConflict(err error) bool {
	ce, ok := AsCallError(err)
	return ok && ce.Category == CategoryConflict
}
func isAmbiguous(err error) bool {
	ce, ok := AsCallError(err)
	return ok && ce.Category == CategoryAmbiguous
}
func isTransient(err error) bool {
	ce, ok := AsCallError(err)
	return ok && ce.Category == CategoryTransient
}
func isRejected(err error) bool {
	ce, ok := AsCallError(err)
	return ok && ce.Category == CategoryRejected
}
func isUnknown(err error) bool {
	ce, ok := AsCallError(err)
	return ok && ce.Category == CategoryUnknown
}
