// Package reconcile implements the desired-state -> store reconciliation
// loop. It owns the policy version assignment boundary: label snapshots and
// policy versions are written together in one store transaction, so an
// evaluator can never read labels from one version and policies from
// another.
package reconcile

import (
	"context"
	"time"

	"netpolicy/internal/domain"
)

// Source supplies the desired state (a fixture adapter, in this project).
type Source interface {
	Fetch(ctx context.Context) (*domain.Snapshot, error)
}

// SnapshotStore is the persistence boundary used by the loop.
type SnapshotStore interface {
	SaveSnapshot(ctx context.Context, snap *domain.Snapshot) (revision int64, changed bool, err error)
	SaveRun(ctx context.Context, r RunRecord) (int64, error)
}

// RunStatus categorizes one loop iteration. Categories are part of the
// contract: independent failure tests assert on them.
type RunStatus string

// Run status categories.
const (
	// StatusApplied: fetch OK, content changed, new revision committed.
	StatusApplied RunStatus = "applied"
	// StatusUnchanged: fetch OK, identical content hash, no new revision.
	StatusUnchanged RunStatus = "unchanged"
	// StatusFetchFailed: the source could not be read or parsed.
	StatusFetchFailed RunStatus = "fetch_failed"
	// StatusValidationFailed: content parsed but violates the model.
	StatusValidationFailed RunStatus = "validation_failed"
	// StatusStoreFailed: a valid snapshot could not be persisted.
	StatusStoreFailed RunStatus = "store_failed"
)

// RunRecord is the persisted audit row for one iteration.
type RunRecord struct {
	ID           int64     `json:"id"`
	StartedAt    time.Time `json:"startedAt"`
	FinishedAt   time.Time `json:"finishedAt"`
	Status       RunStatus `json:"status"`
	Revision     int64     `json:"revision"`
	ContentHash  string    `json:"contentHash"`
	ErrorKind    string    `json:"errorKind,omitempty"`
	ErrorMessage string    `json:"errorMessage,omitempty"`
	Attempted    bool      `json:"attempted"`
}

// Reconciler runs Fetch -> Validate(via source) -> Save -> Record.
type Reconciler struct {
	src   Source
	store SnapshotStore
	now   func() time.Time
}

// New builds a reconciler.
func New(src Source, store SnapshotStore) *Reconciler {
	return &Reconciler{src: src, store: store, now: time.Now}
}

// Once performs a single reconciliation pass and returns its record. It
// never returns a fetch/validation error to the caller: those are classified
// into the record. Store errors are returned because they indicate the audit
// log itself cannot be written.
func (r *Reconciler) Once(ctx context.Context) (rec RunRecord, err error) {
	rec = RunRecord{StartedAt: r.now(), Attempted: true}
	// finish stamps the end time on both the persisted row and the returned
	// record, so diagnostics never show a zero finishedAt.
	finish := func() { rec.FinishedAt = r.now() }

	snap, fetchErr := r.src.Fetch(ctx)
	if fetchErr != nil {
		rec.Status = classifyFetchError(fetchErr)
		rec.ErrorKind, rec.ErrorMessage = errorParts(fetchErr)
		finish()
		id, saveErr := r.store.SaveRun(ctx, rec)
		if saveErr != nil {
			return rec, saveErr
		}
		rec.ID = id
		return rec, nil
	}
	rec.ContentHash = snap.SourceHash

	rev, changed, storeErr := r.store.SaveSnapshot(ctx, snap)
	if storeErr != nil {
		rec.Status = StatusStoreFailed
		rec.ErrorKind = "store_write_failed"
		rec.ErrorMessage = storeErr.Error()
		finish()
		id, saveErr := r.store.SaveRun(ctx, rec)
		if saveErr != nil {
			return rec, saveErr
		}
		rec.ID = id
		return rec, nil
	}
	rec.Revision = rev
	if changed {
		rec.Status = StatusApplied
	} else {
		rec.Status = StatusUnchanged
	}
	finish()
	id, storeErr := r.store.SaveRun(ctx, rec)
	if storeErr != nil {
		return rec, storeErr
	}
	rec.ID = id
	return rec, nil
}

func classifyFetchError(err error) RunStatus {
	if ve, ok := domain.AsValidationError(err); ok {
		if ve.Kind == domain.ErrSourceNotFound || ve.Kind == domain.ErrSourceSyntax {
			return StatusFetchFailed
		}
		return StatusValidationFailed
	}
	return StatusFetchFailed
}

func errorParts(err error) (string, string) {
	if ve, ok := domain.AsValidationError(err); ok {
		return string(ve.Kind), ve.Error()
	}
	return "fetch_failed", err.Error()
}
