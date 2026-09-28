// Package reconcile implements the controller's reconciliation loop.
//
// One reconcile pass drives a single object through this state machine:
//
//	terminating? -> delete external (retry until gone) -> remove finalizer
//	            -> desired plane purges the record -> clear local state
//	not terminating -> ensure finalizer -> ensure external resource
//	            -> claim-after-lost-create instead of duplicate create
//	            -> observe actual; reject stale/future observations
//	            -> update external spec when generation advanced
//	            -> write status with optimistic concurrency; requeue on 409
//
// Every decision is appended to a ledger with a reason code, failure
// category, generations, versions and the correlation request ID, so tests can
// assert exactly why a step was accepted, rejected or undecidable.
package reconcile

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"time"

	"crcontroller/internal/actual"
	"crcontroller/internal/controllerstore"
	"crcontroller/internal/desired"
	"crcontroller/internal/logx"
	"crcontroller/internal/model"
)

// Config configures a Controller.
type Config struct {
	Desired          *desired.Client
	Actual           *actual.Client
	Store            *controllerstore.Store
	Log              *logx.Logger
	Backoff          Backoff
	ResyncInterval   time.Duration
	RequeueDelay     time.Duration
	ExternalIDPrefix string
}

// requeueError is a sentinel for expected multi-round transitions (finalizer
// just added, update just applied, ...). It is not a failure: the retry
// counter is not incremented and the key is requeued, optionally after a
// short delay to avoid a hot loop.
type requeueError struct {
	reason string
	delay  time.Duration
}

func (e *requeueError) Error() string { return "requeue: " + e.reason }

func requeue(reason string) error {
	return &requeueError{reason: reason}
}

func requeueAfter(reason string, d time.Duration) error {
	return &requeueError{reason: reason, delay: d}
}

func asRequeue(err error) (*requeueError, bool) {
	var rq *requeueError
	if errors.As(err, &rq) {
		return rq, true
	}
	return nil, false
}

// Controller runs the reconcile loop.
type Controller struct {
	cfg Config
	q   *Queue

	mu        sync.Mutex
	retries   map[string]int
	processed map[string]int64 // total completed processing rounds per key
	started   bool
}

// New constructs a controller.
func New(cfg Config) *Controller {
	if cfg.Backoff.Base == 0 {
		cfg.Backoff = Backoff{Base: 50 * time.Millisecond, Max: 5 * time.Second}
	}
	if cfg.ResyncInterval == 0 {
		cfg.ResyncInterval = 30 * time.Second
	}
	if cfg.RequeueDelay == 0 {
		cfg.RequeueDelay = 25 * time.Millisecond
	}
	if cfg.ExternalIDPrefix == "" {
		cfg.ExternalIDPrefix = "res"
	}
	return &Controller{
		cfg:       cfg,
		q:         NewQueue(cfg.Backoff),
		retries:   map[string]int{},
		processed: map[string]int64{},
	}
}

// Queue exposes the work queue (the in-process API server forwards change
// events to Enqueue).
func (c *Controller) Queue() *Queue { return c.q }

// Enqueue is the event entry point: spec changes, finalizer changes and
// deletions all funnel here. Duplicate calls are coalesced by the queue.
func (c *Controller) Enqueue(uid string) { c.q.Add(uid) }

// Retries returns the current retry count for a key.
func (c *Controller) Retries(uid string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.retries[uid]
}

// ProcessedRounds returns how many reconcile rounds finished for uid.
func (c *Controller) ProcessedRounds(uid string) int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.processed[uid]
}

// Start runs the worker and the periodic resync until ctx is cancelled.
func (c *Controller) Start(ctx context.Context) {
	c.mu.Lock()
	if c.started {
		c.mu.Unlock()
		return
	}
	c.started = true
	c.mu.Unlock()

	go c.worker(ctx)
	go c.resync(ctx)
}

func (c *Controller) worker(ctx context.Context) {
	for {
		key, ok := c.q.Get()
		if !ok {
			return
		}
		// A per-round request ID ties this pass's downstream calls together.
		rid := newRoundID()
		err := c.reconcile(ctx, key, rid)
		c.q.Done(key)
		if err != nil {
			if rq, ok := asRequeue(err); ok {
				// Expected multi-round transition: no retry/backoff counter.
				delay := rq.delay
				if delay <= 0 {
					delay = c.cfg.RequeueDelay
				}
				if delay <= 0 {
					c.q.Add(key)
				} else {
					c.q.AddAfter(key, delay)
				}
				continue
			}
			c.mu.Lock()
			c.retries[key]++
			n := c.retries[key]
			c.mu.Unlock()
			c.q.AddRateLimited(key, n)
			continue
		}
		c.mu.Lock()
		c.retries[key] = 0
		c.processed[key]++
		c.mu.Unlock()
	}
}

func (c *Controller) resync(ctx context.Context) {
	t := time.NewTicker(c.cfg.ResyncInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			objs, err := c.cfg.Desired.List(ctx, newRoundID())
			if err != nil {
				c.cfg.Log.Warn("reconcile", "resync list failed",
					map[string]any{"error": err.Error()})
				continue
			}
			for _, o := range objs {
				c.q.Add(o.UID)
			}
		}
	}
}

// reconcile drives one pass. It returns an error when the key must be
// retried; nil means the object was observed at the desired state (or was
// purged), and no immediate retry is needed.
func (c *Controller) reconcile(ctx context.Context, uid, rid string) error {
	st, err := c.cfg.Store.GetState(ctx, uid)
	if err != nil {
		return c.failRead(ctx, uid, rid, err)
	}
	attempt, err := c.cfg.Store.IncrementAttempts(ctx, uid)
	if err != nil {
		return err
	}

	obj, err := c.cfg.Desired.Get(ctx, uid, rid)
	if err != nil {
		var de *desired.Error
		if errors.As(err, &de) && de.Kind == desired.KindNotFound {
			// Record may already be purged. Clean local bookkeeping.
			_ = c.cfg.Store.ClearState(ctx, uid)
			c.ledger(ctx, uid, attempt, "finalize", model.DecSettled,
				model.CatNone, "object absent from desired plane; settled",
				model.LedgerEntry{RequestID: rid})
			return nil
		}
		return c.failRead(ctx, uid, rid, err)
	}

	c.logRound(obj, rid, attempt)

	// Branch 1: deletion. The record leaves the database only after the
	// external resource is gone and the finalizer is removed.
	if obj.Terminating() {
		return c.reconcileDelete(ctx, obj, attempt, st, rid)
	}
	return c.reconcilePresent(ctx, obj, attempt, st, rid)
}

// reconcilePresent handles a live object.
func (c *Controller) reconcilePresent(ctx context.Context, obj *model.Object,
	attempt int64, st controllerstore.State, rid string) error {
	// Step 1: ensure the finalizer so a crash before external cleanup cannot
	// orphan the physical resource.
	if !obj.HasFinalizer() {
		updated, err := c.cfg.Desired.PatchFinalizers(ctx, desired.FinalizerAction{
			RequestID: rid, Namespace: obj.Namespace, Name: obj.Name,
			Action: "add", ResourceVersion: obj.ResourceVer,
		})
		if err != nil {
			return c.handleDesiredWriteError(ctx, obj, attempt, st,
				"finalizer/add", model.DecFinalizerAdded, err, rid)
		}
		c.ledgerFromObj(ctx, updated, attempt, "finalizer",
			model.DecFinalizerAdded, model.CatNone,
			"finalizer added; requeue before external work", rid, st.LastActualVersion)
		// Requeue immediately: work from the new resourceVersion next round.
		return requeue("finalizer added")
	}

	// Step 2: locate the physical resource, claiming it after a lost create.
	res, err := c.ensureExternal(ctx, obj, attempt, st, rid)
	if err != nil {
		return err
	}

	// Step 3: observation gating. Reject stale snapshots and impossible
	// generations; accept only observations at least as new as what we have
	// already acted on.
	r := res.Resource
	if r.Generation > obj.Generation {
		c.ledgerFromObj(ctx, obj, attempt, "observe", model.DecObservedAhead,
			model.CatRefused,
			fmt.Sprintf("external generation %d ahead of desired %d; undecidable",
				r.Generation, obj.Generation),
			rid, r.Version)
		return requeueAfter("observation ahead of desired", 30*time.Millisecond)
	}

	staleReason := ""
	if res.ServedStale && r.Version <= st.LastActualVersion {
		staleReason = fmt.Sprintf(
			"served stale snapshot version %d <= last accepted %d (current actual %d)",
			r.Version, st.LastActualVersion, res.CurrentVersion)
	} else if r.Version < st.LastActualVersion {
		staleReason = fmt.Sprintf(
			"observed version %d older than last accepted %d",
			r.Version, st.LastActualVersion)
	}
	if staleReason != "" {
		c.ledgerFromObj(ctx, obj, attempt, "observe", model.DecObservedStale,
			model.CatStaleObservation, staleReason, rid, r.Version)
		// Not an external error: do not mutate, wait for a fresh observation
		// (resync/next event will provide it).
		return requeueAfter("stale observation", 30*time.Millisecond)
	}

	// Step 4: drive actual spec forward when desired generation advanced.
	needUpdate := r.Generation < obj.Generation || r.SpecHash != obj.SpecHash
	if needUpdate {
		updated, uerr := c.cfg.Actual.Update(ctx, actual.UpdateRequest{
			RequestID: rid, ID: r.ID, ExpectedVer: r.Version,
			Generation: obj.Generation, SpecHash: obj.SpecHash, Spec: obj.Spec,
		})
		if uerr != nil {
			return c.handleActualMutationError(ctx, obj, attempt, st,
				r, "update", model.DecUpdateRequested,
				model.DecUpdateConflict, uerr, rid)
		}
		if err := c.cfg.Store.RecordObservation(ctx, obj.UID, updated.ID,
			updated.Version, updated.Generation); err != nil {
			return err
		}
		st.LastActualVersion = updated.Version
		c.ledgerFromObj(ctx, obj, attempt, "update", model.DecUpdateRequested,
			model.CatNone,
			fmt.Sprintf("external updated to generation %d version %d",
				updated.Generation, updated.Version),
			rid, updated.Version)
		// Re-read actual state through a fresh observation next round so a
		// status write reflects the applied version.
		return requeue("update applied")
	}

	// Step 5: observed generation matches desired. Persist status.
	if r.Generation != obj.Generation {
		// Generation behind but hashes equal is still a not-yet-converged
		// observation: undecidable, requeue.
		c.ledgerFromObj(ctx, obj, attempt, "observe", model.DecObservedStale,
			model.CatStaleObservation,
			fmt.Sprintf("external gen %d behind desired %d despite matching hash",
				r.Generation, obj.Generation),
			rid, r.Version)
		return requeue("observation lagging")
	}

	if err := c.cfg.Store.RecordObservation(ctx, obj.UID, r.ID,
		r.Version, r.Generation); err != nil {
		return err
	}
	// Idempotent settle: if status already reflects this exact observation,
	// a periodic resync must not rewrite it (which would bump
	// resourceVersion and cause pointless churn). Only write when something
	// actually changed.
	if statusAlreadySettled(obj, r) {
		c.ledgerFromObj(ctx, obj, attempt, "settle", model.DecSettled,
			model.CatNone,
			fmt.Sprintf("already settled at generation %d (actual version %d); no status write",
				r.Generation, r.Version),
			rid, r.Version)
		return nil
	}
	if err := c.writeReadyStatus(ctx, obj, r, attempt, rid); err != nil {
		return err
	}
	c.ledgerFromObj(ctx, obj, attempt, "settle", model.DecObservedInSync,
		model.CatNone,
		fmt.Sprintf("observed generation %d applied (actual version %d)",
			r.Generation, r.Version),
		rid, r.Version)
	return nil
}

// statusAlreadySettled reports the object's status already matches the
// observation that a status write would produce.
func statusAlreadySettled(obj *model.Object, r *model.ActualResource) bool {
	st := obj.Status
	if st.ObservedGeneration != obj.Generation || st.ExternalID != r.ID {
		return false
	}
	if st.State != r.State {
		return false
	}
	var ready *model.Condition
	for i := range st.Conditions {
		if st.Conditions[i].Type == model.ConditionExternalReady {
			ready = &st.Conditions[i]
		}
	}
	return ready != nil && ready.Status == model.CondTrue &&
		ready.ObservedGeneration == obj.Generation
}

// ensureExternal returns the observation of the physical resource, creating
// it if absent. A lost create response is handled by querying by owner UID
// and claiming the existing row — a duplicate physical resource is never
// created.
func (c *Controller) ensureExternal(ctx context.Context, obj *model.Object,
	attempt int64, st controllerstore.State, rid string) (*actual.Observation, error) {
	if st.ExternalID != "" {
		obs, err := c.cfg.Actual.Get(ctx, st.ExternalID, rid)
		if err == nil {
			return obs, nil
		}
		var ae *actual.Error
		if errors.As(err, &ae) && ae.Kind == actual.KindNotFound {
			// External resource vanished out of band: treat as missing and
			// recreate below, recording the ownership gap.
			c.ledgerFromObj(ctx, obj, attempt, "observe",
				model.DecExternalMissing, model.CatNotFound,
				"external resource missing; will recreate", rid, 0)
		} else {
			return nil, err
		}
	}

	// Claim path first: query by owner so an earlier committed create whose
	// response was lost is adopted rather than duplicated.
	existing, err := c.cfg.Actual.GetByOwner(ctx, obj.UID, rid)
	if err == nil {
		if err := c.cfg.Store.RecordObservation(ctx, obj.UID, existing.ID,
			existing.Version, existing.Generation); err != nil {
			return nil, err
		}
		c.ledgerFromObj(ctx, obj, attempt, "create", model.DecClaimed,
			model.CatNone,
			fmt.Sprintf("claimed existing %s after query by owner (version %d)",
				existing.ID, existing.Version),
			rid, existing.Version)
		return &actual.Observation{Resource: existing, CurrentVersion: existing.Version}, nil
	}
	var qe *actual.Error
	if !errors.As(err, &qe) || qe.Kind != actual.KindNotFound {
		return nil, err
	}

	// No existing resource: create.
	id := c.newExternalID(obj)
	created, cerr := c.cfg.Actual.Create(ctx, actual.CreateResourceRequest{
		RequestID: rid, ID: id, OwnerUID: obj.UID,
		Generation: obj.Generation, SpecHash: obj.SpecHash, Spec: obj.Spec,
	})
	if cerr == nil {
		if err := c.cfg.Store.RecordObservation(ctx, obj.UID, created.ID,
			created.Version, created.Generation); err != nil {
			return nil, err
		}
		c.ledgerFromObj(ctx, obj, attempt, "create",
			model.DecCreateRequested, model.CatNone,
			fmt.Sprintf("created %s at generation %d", created.ID, created.Generation),
			rid, created.Version)
		return &actual.Observation{Resource: created, CurrentVersion: created.Version}, nil
	}

	var ce *actual.Error
	if errors.As(cerr, &ce) {
		switch ce.Kind {
		case actual.KindResponseLost:
			// Committed-but-lost: record the undecidable mutation first, then
			// resolve it by claiming the existing row by owner lookup.
			c.ledgerFromObj(ctx, obj, attempt, "create",
				model.DecCreateResponseLost, model.CatResponseLost,
				fmt.Sprintf("create response lost after commit (requestID %s); querying by owner",
					ce.RequestID),
				rid, 0)
			claimed, lerr := c.cfg.Actual.GetByOwner(ctx, obj.UID, rid)
			if lerr != nil {
				return nil, lerr
			}
			if err := c.cfg.Store.RecordObservation(ctx, obj.UID, claimed.ID,
				claimed.Version, claimed.Generation); err != nil {
				return nil, err
			}
			c.ledgerFromObj(ctx, obj, attempt, "create",
				model.DecClaimed, model.CatNone,
				fmt.Sprintf("claimed %s by owner lookup after lost create response",
					claimed.ID),
				rid, claimed.Version)
			return &actual.Observation{Resource: claimed, CurrentVersion: claimed.Version}, nil
		case actual.KindAlreadyOwned:
			// Racy double-create: body carries the existing row.
			if created != nil {
				if err := c.cfg.Store.RecordObservation(ctx, obj.UID, created.ID,
					created.Version, created.Generation); err != nil {
					return nil, err
				}
				c.ledgerFromObj(ctx, obj, attempt, "create",
					model.DecClaimed, model.CatNone,
					"create returned AlreadyOwned; claimed existing",
					rid, created.Version)
				return &actual.Observation{Resource: created, CurrentVersion: created.Version}, nil
			}
		case actual.KindTransient:
			c.ledgerFromObj(ctx, obj, attempt, "create",
				model.DecCreateRequested, model.CatTransient,
				"create transient failure: "+ce.Error(), rid, 0)
			return nil, cerr
		default:
			c.ledgerFromObj(ctx, obj, attempt, "create",
				model.DecUnexpectedError, model.CatTransient,
				"create failed: "+ce.Error(), rid, 0)
			return nil, cerr
		}
	}
	return nil, cerr
}

// reconcileDelete drives external deletion and finalizer removal.
func (c *Controller) reconcileDelete(ctx context.Context, obj *model.Object,
	attempt int64, st controllerstore.State, rid string) error {
	externalID := st.ExternalID
	if externalID == "" {
		externalID = obj.Status.ExternalID
	}

	if externalID != "" {
		// Confirm whether the physical resource still exists.
		obs, gerr := c.cfg.Actual.Get(ctx, externalID, rid)
		switch {
		case gerr == nil:
			// Still present: delete. A lost delete response is resolved by a
			// follow-up GET (delete is idempotent); a plain injected failure
			// leaves the row and is retried.
			derr := c.cfg.Actual.Delete(ctx, obs.Resource.ID, 0, rid)
			if derr == nil {
				c.ledgerFromObj(ctx, obj, attempt, "delete",
					model.DecDeleteRequested, model.CatNone,
					"external resource deleted", rid, obs.Resource.Version)
				return requeue("external deleted")
			}
			var de *actual.Error
			if errors.As(derr, &de) {
				switch de.Kind {
				case actual.KindResponseLost:
					c.ledgerFromObj(ctx, obj, attempt, "delete",
						model.DecDeleteResponseLost, model.CatResponseLost,
						"delete response lost; will confirm by GET next round",
						rid, obs.Resource.Version)
				case actual.KindFaultInjected:
					c.ledgerFromObj(ctx, obj, attempt, "delete",
						model.DecDeleteRequested, model.CatTransient,
						"delete failed (injected), row kept; retrying",
						rid, obs.Resource.Version)
				case actual.KindNotFound:
					// already gone; proceed
				default:
					c.ledgerFromObj(ctx, obj, attempt, "delete",
						model.DecUnexpectedError, model.CatTransient,
						"delete error: "+de.Error(), rid, obs.Resource.Version)
				}
			}
			return derr
		default:
			var de *actual.Error
			if !errors.As(gerr, &de) || de.Kind != actual.KindNotFound {
				return gerr
			}
			// Confirmed gone.
			c.ledgerFromObj(ctx, obj, attempt, "delete",
				model.DecDeleteRequested, model.CatNone,
				"external resource confirmed absent", rid, 0)
		}
	}

	// External cleanup complete: remove the finalizer (guarded). The desired
	// plane physically purges the record once finalizers are empty.
	if obj.HasFinalizer() {
		updated, err := c.cfg.Desired.PatchFinalizers(ctx, desired.FinalizerAction{
			RequestID: rid, Namespace: obj.Namespace, Name: obj.Name,
			Action: "remove", ResourceVersion: obj.ResourceVer,
		})
		if err != nil {
			return c.handleDesiredWriteError(ctx, obj, attempt, st,
				"finalizer/remove", model.DecFinalizerRemoved, err, rid)
		}
		c.ledgerFromObj(ctx, updated, attempt, "finalizer",
			model.DecFinalizerRemoved, model.CatNone,
			"finalizer removed after external cleanup", rid, 0)
		return requeue("finalizer removed")
	}

	// No finalizer: ask the desired plane to physically remove the record.
	// A DELETE is idempotent: 404 means it is already purged.
	_, err := c.cfg.Desired.Delete(ctx, obj.UID, rid)
	if err != nil {
		var de *desired.Error
		if errors.As(err, &de) && de.Kind == desired.KindNotFound {
			if cerr := c.cfg.Store.ClearState(ctx, obj.UID); cerr != nil {
				return cerr
			}
			c.ledgerFromObj(ctx, obj, attempt, "finalize",
				model.DecSettled, model.CatNone,
				"record purged and local state cleared", rid, 0)
			return nil
		}
		return err
	}
	if cerr := c.cfg.Store.ClearState(ctx, obj.UID); cerr != nil {
		return cerr
	}
	c.ledgerFromObj(ctx, obj, attempt, "finalize",
		model.DecSettled, model.CatNone,
		"record purge requested after finalizer removal", rid, 0)
	return nil
}

// writeReadyStatus writes the converged status. A 409 means the spec moved
// under us: requeue rather than overwrite the new generation.
func (c *Controller) writeReadyStatus(ctx context.Context, obj *model.Object,
	res *model.ActualResource, attempt int64, rid string) error {
	conds := []model.Condition{
		{
			Type: model.ConditionExternalReady, Status: model.CondTrue,
			Reason: "Applied", Message: "external resource matches desired spec",
			ObservedGeneration: obj.Generation,
			LastTransition:     time.Now().UTC(),
		},
		{
			Type:               model.ConditionFinalizersPresent,
			Status:             boolStatus(obj.HasFinalizer()),
			Reason:             "FinalizerSet",
			ObservedGeneration: obj.Generation,
			LastTransition:     time.Now().UTC(),
		},
	}
	updated, err := c.cfg.Desired.PutStatus(ctx, desired.StatusWrite{
		RequestID: rid, Namespace: obj.Namespace, Name: obj.Name,
		ResourceVersion:    obj.ResourceVer,
		ObservedGeneration: obj.Generation, ExternalID: res.ID,
		State: res.State, Conditions: conds,
	})
	if err != nil {
		return c.handleDesiredWriteError(ctx, obj, attempt,
			controllerstore.State{ExternalID: res.ID, LastActualVersion: res.Version},
			"status", model.DecStatusWritten, err, rid)
	}
	c.ledgerFromObj(ctx, updated, attempt, "status",
		model.DecStatusWritten, model.CatNone,
		fmt.Sprintf("status written observedGeneration=%d rv=%d",
			updated.Generation, updated.ResourceVer),
		rid, res.Version)
	return nil
}

func boolStatus(v bool) model.ConditionStatus {
	if v {
		return model.CondTrue
	}
	return model.CondFalse
}

// handleDesiredWriteError maps 409/422 from the desired plane to ledger
// categories and returns a retry error where appropriate.
func (c *Controller) handleDesiredWriteError(ctx context.Context, obj *model.Object,
	attempt int64, st controllerstore.State, phase, decision string,
	err error, rid string) error {
	var de *desired.Error
	cat := model.CatTransient
	dec := decision
	detail := err.Error()
	retryable := true
	if errors.As(err, &de) {
		switch de.Kind {
		case desired.KindConflict:
			cat = model.CatConflict
			if phase == "status" {
				dec = model.DecStatusConflict
				detail = "status write lost optimistic concurrency; requeue, new spec preserved"
			} else {
				dec = model.DecUpdateConflict
				detail = "write conflict; requeue without overwriting newer spec"
			}
		case desired.KindRefused:
			cat = model.CatRefused
			retryable = false
		case desired.KindTerminating:
			cat = model.CatConflict
		case desired.KindUnauthorized:
			cat = model.CatRefused
			detail = "controller credential rejected: " + de.Error()
		case desired.KindNotFound:
			cat = model.CatNotFound
			retryable = false
		}
	}
	c.ledgerFromObj(ctx, obj, attempt, phase, dec, cat, detail, rid,
		st.LastActualVersion)
	if retryable {
		return err
	}
	return nil
}

// handleActualMutationError maps update conflicts / lost responses.
func (c *Controller) handleActualMutationError(ctx context.Context, obj *model.Object,
	attempt int64, st controllerstore.State, res *model.ActualResource,
	phase, decisionOK, decisionConflict string, err error, rid string) error {
	var ae *actual.Error
	cat := model.CatTransient
	dec := decisionOK
	detail := err.Error()
	if errors.As(err, &ae) {
		switch ae.Kind {
		case actual.KindVersionConflict:
			cat = model.CatConflict
			dec = decisionConflict
			detail = "external version conflict; requeue and re-observe"
		case actual.KindResponseLost:
			cat = model.CatResponseLost
			detail = "mutation response lost; verify by observation next round"
		case actual.KindNotFound:
			cat = model.CatNotFound
		}
	}
	ver := int64(0)
	if res != nil {
		ver = res.Version
	}
	c.ledgerFromObj(ctx, obj, attempt, phase, dec, cat, detail, rid, ver)
	return err
}

func (c *Controller) failRead(ctx context.Context, uid, rid string, err error) error {
	c.cfg.Log.Warn("reconcile", "desired-plane read failed", map[string]any{
		"uid": uid, "requestID": rid, "error": err.Error(),
	})
	return err
}

// ledgerFromObj appends a decision filling generations/version from obj.
func (c *Controller) ledgerFromObj(ctx context.Context, obj *model.Object,
	attempt int64, phase, decision string, cat model.FailureCategory,
	detail, rid string, actualVersion int64) {
	c.ledger(ctx, obj.UID, attempt, phase, decision, cat, detail,
		model.LedgerEntry{
			ExternalID:         obj.Status.ExternalID,
			DesiredGeneration:  obj.Generation,
			ObservedGeneration: obj.Status.ObservedGeneration,
			ResourceVersion:    obj.ResourceVer,
			ActualVersion:      actualVersion,
			RequestID:          rid,
		})
}

// ledger appends a free-form decision entry to the controller database.
func (c *Controller) ledger(ctx context.Context, uid string, attempt int64,
	phase, decision string, cat model.FailureCategory, detail string,
	base model.LedgerEntry) {
	base.UID = uid
	base.Attempt = attempt
	base.Phase = phase
	base.Decision = decision
	base.Category = cat
	base.Detail = detail
	if _, err := c.cfg.Store.AppendLedger(ctx, base); err != nil {
		c.cfg.Log.Error("reconcile", "ledger append failed", map[string]any{
			"uid": uid, "error": err.Error(),
		})
	}
	c.cfg.Log.Info("reconcile", "decision", map[string]any{
		"uid": uid, "attempt": attempt, "phase": phase,
		"decision": decision, "category": string(cat),
		"detail": detail, "requestID": base.RequestID,
		"desiredGeneration":  base.DesiredGeneration,
		"observedGeneration": base.ObservedGeneration,
		"actualVersion":      base.ActualVersion,
	})
}

func (c *Controller) logRound(obj *model.Object, rid string, attempt int64) {
	c.cfg.Log.Info("reconcile", "reconcile start", map[string]any{
		"uid": obj.UID, "requestID": rid, "attempt": attempt,
		"generation": obj.Generation, "observedGeneration": obj.Status.ObservedGeneration,
		"resourceVersion": obj.ResourceVer, "terminating": obj.Terminating(),
		"finalizers": obj.Finalizers, "spec": obj.Spec,
	})
}

func (c *Controller) newExternalID(obj *model.Object) string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return fmt.Sprintf("%s-%s-%s", c.cfg.ExternalIDPrefix,
		obj.Namespace, hex.EncodeToString(b[:]))
}

func newRoundID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "ctl-" + hex.EncodeToString(b[:])
}
