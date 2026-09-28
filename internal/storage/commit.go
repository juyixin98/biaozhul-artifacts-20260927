package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"admission/internal/model"
	"admission/internal/plugins"
)

// AuditRecord is the persisted, replayable record of one admission: the
// request, final verdict, final-object summary and every plugin step.
type AuditRecord struct {
	RunID  string         `json:"runId"`
	Status string         `json:"status"`
	Reason model.Reason   `json:"reason,omitempty"`
	Resp   model.Response `json:"response"`
}

// CommitOutcome persists a finished admission transactionally:
//
//   - allowed: the resource upsert, quota hold commit and terminal request
//     state happen in one SQLite transaction. A name collision on CREATE is a
//     state_conflict; a race is therefore impossible with an exhausted quota
//     check (same tx). Any failure rolls everything back.
//   - denied/failed: quota holds recorded during validation are revoked and
//     the request reaches its terminal state with the audit trail attached.
//
// The summary of the final object (or the reason why none exists) is bound to
// both the request row and the audit row.
func (s *Store) CommitOutcome(ctx context.Context, runID string, req model.Request, resp model.Response, nowMs int64) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	status := StatusDenied
	if resp.Decision == model.DecisionAllowed {
		status = StatusAllowed
	}
	digest := ""
	if resp.FinalSummary != nil {
		digest = resp.FinalSummary.Digest
	}

	if resp.Decision == model.DecisionAllowed {
		// persistResourceTx performs the authoritative capacity check INSIDE
		// this transaction: it upserts/deletes the resource and then sums the
		// real committed replicas plus uncommitted in-flight holds of other
		// requests. Concurrent admits serialize (single writer), so over-limit
		// admits can never both commit. The validator-time reserve is only an
		// early rejection hint.
		if err := s.persistResourceTx(ctx, tx, req, resp, nowMs); err != nil {
			return err
		}
		// The hold for this uid has been consumed by the authoritative check;
		// remove it so the ledger only ever contains in-flight reservations.
		if _, err := tx.ExecContext(ctx, `DELETE FROM quota_ledger WHERE uid = ?`, req.UID); err != nil {
			return err
		}
	} else if err := s.releaseHoldsTx(ctx, tx, req.UID); err != nil {
		return err
	}

	if _, err := tx.ExecContext(ctx,
		`UPDATE requests SET status = ?, reason = ?, message = ?, final_digest = ?, lease_until = 0, updated_at = ?
		 WHERE uid = ?`,
		status, string(resp.Reason), resp.Message, digest, nowMs, req.UID); err != nil {
		return err
	}

	if err := s.writeAuditTx(ctx, tx, runID, status, resp, nowMs); err != nil {
		return err
	}
	return tx.Commit()
}

// MarkFailed records an infrastructure failure outside the verdict model
// (e.g. request JSON corruption). Quota holds cannot exist for such requests;
// the reconciler retries them after lease expiry.
func (s *Store) MarkFailed(ctx context.Context, uid, msg string, nowMs int64) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE requests SET status = ?, reason = ?, message = ?, lease_until = 0, updated_at = ?
		 WHERE uid = ?`,
		StatusFailed, string(model.ReasonComputeFailure), msg, nowMs, uid)
	return err
}

func (s *Store) persistResourceTx(ctx context.Context, tx *sql.Tx, req model.Request, resp model.Response, nowMs int64) error {
	res, err := model.ResourceFromMap(resp.FinalObject)
	if err != nil {
		return fmt.Errorf("encode committed resource: %w", err)
	}
	doc, err := json.Marshal(resp.FinalObject)
	if err != nil {
		return err
	}
	switch req.Operation {
	case model.OpCreate:
		if err := s.upsertResource(ctx, tx, req.UID, res, doc, nowMs, true); err != nil {
			return err
		}
		return s.checkCapacityTx(ctx, tx, res.Kind, res.Metadata.Namespace, res.Metadata.Name, req.UID)
	case model.OpUpdate:
		if err := s.upsertResource(ctx, tx, req.UID, res, doc, nowMs, false); err != nil {
			return err
		}
		return s.checkCapacityTx(ctx, tx, res.Kind, res.Metadata.Namespace, res.Metadata.Name, req.UID)
	case model.OpDelete:
		_, derr := tx.ExecContext(ctx,
			`DELETE FROM resources WHERE kind = ? AND namespace = ? AND name = ?`,
			res.Kind, res.Metadata.Namespace, res.Metadata.Name)
		return derr // deletion only frees capacity: no check
	}
	return nil
}

// checkCapacityTx is the authoritative admission-time capacity gate. It runs
// after the resource mutation, inside the commit transaction, so its view is
// the one that becomes durable:
//
//	total = committed replicas of all resources of this kind
//	      + in-flight holds of OTHER requests (CREATE full amount, UPDATE delta)
//
// The committing request's own hold is excluded (it is already represented by
// the just-upserted resource row). Single-writer serialization guarantees two
// admits cannot both pass this gate.
func (s *Store) checkCapacityTx(ctx context.Context, tx *sql.Tx, kind, namespace, name, uid string) error {
	limit, limited := s.quotaLimits[kind]
	if !limited {
		return nil
	}
	var committed int64
	if err := tx.QueryRowContext(ctx,
		`SELECT COALESCE(SUM(replicas), 0) FROM resources WHERE kind = ?`, kind).Scan(&committed); err != nil {
		return err
	}
	var otherHolds int64
	if err := tx.QueryRowContext(ctx,
		`SELECT COALESCE(SUM(delta), 0) FROM quota_ledger WHERE kind = ? AND uid <> ?`, kind, uid).
		Scan(&otherHolds); err != nil {
		return err
	}
	if committed+otherHolds > limit {
		return fmt.Errorf("%w: kind=%s committed=%d otherHolds=%d limit=%d (%s/%s)",
			plugins.ErrQuotaExhausted, kind, committed, otherHolds, limit, namespace, name)
	}
	return nil
}

func (s *Store) upsertResource(ctx context.Context, tx *sql.Tx, uid string,
	res model.Resource, doc []byte, nowMs int64, createOnly bool) error {
	var replicas int64
	if n, ok := res.Spec["replicas"]; ok {
		if f, ok := n.(float64); ok && f == float64(int64(f)) {
			replicas = int64(f)
		}
	}
	if createOnly {
		_, err := tx.ExecContext(ctx,
			`INSERT INTO resources (kind, namespace, name, uid, doc, replicas, updated_at)
			 VALUES (?, ?, ?, ?, ?, ?, ?)`,
			res.Kind, res.Metadata.Namespace, res.Metadata.Name, uid, string(doc), replicas, nowMs)
		if err != nil && isUniqueViolation(err) {
			return ErrConflict
		}
		return err
	}
	res2, err := tx.ExecContext(ctx,
		`UPDATE resources SET uid = ?, doc = ?, replicas = ?, updated_at = ?
		 WHERE kind = ? AND namespace = ? AND name = ?`,
		uid, string(doc), replicas, nowMs,
		res.Kind, res.Metadata.Namespace, res.Metadata.Name)
	if err != nil {
		return err
	}
	n, _ := res2.RowsAffected()
	if n == 0 {
		return fmt.Errorf("%w: update target %s/%s not found", ErrNotFound, res.Kind, res.Metadata.Name)
	}
	return nil
}

func (s *Store) releaseHoldsTx(ctx context.Context, tx *sql.Tx, uid string) error {
	if _, err := tx.ExecContext(ctx, `DELETE FROM quota_ledger WHERE uid = ?`, uid); err != nil {
		return err
	}
	return nil
}

func (s *Store) writeAuditTx(ctx context.Context, tx *sql.Tx, runID, status string, resp model.Response, nowMs int64) error {
	rec := AuditRecord{RunID: runID, Status: status, Reason: resp.Reason, Resp: resp}
	raw, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	digest := ""
	if resp.FinalSummary != nil {
		digest = resp.FinalSummary.Digest
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO audits (uid, run_id, status, reason, summary_digest, record, created_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		resp.UID, runID, status, string(resp.Reason), digest, string(raw), nowMs)
	return err
}

// LoadAudits returns the audit records for a uid (newest first) — the replay
// surface used by tests and by GET /v1/requests/{uid}/audits.
func (s *Store) LoadAudits(ctx context.Context, uid string) ([]AuditRecord, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT record FROM audits WHERE uid = ? ORDER BY id DESC`, uid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AuditRecord
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var rec AuditRecord
		if err := json.Unmarshal([]byte(raw), &rec); err != nil {
			return nil, err
		}
		out = append(out, rec)
	}
	return out, rows.Err()
}

// ResourceRow is one committed resource.
type ResourceRow struct {
	Kind, Namespace, Name, UID, Doc string
	Replicas                        int64
}

// GetResource loads a committed resource by identity.
func (s *Store) GetResource(ctx context.Context, kind, namespace, name string) (ResourceRow, error) {
	var r ResourceRow
	err := s.db.QueryRowContext(ctx,
		`SELECT kind, namespace, name, uid, doc, replicas FROM resources
		 WHERE kind = ? AND namespace = ? AND name = ?`, kind, namespace, name).
		Scan(&r.Kind, &r.Namespace, &r.Name, &r.UID, &r.Doc, &r.Replicas)
	if errors.Is(err, sql.ErrNoRows) {
		return ResourceRow{}, fmt.Errorf("%w: %s/%s/%s", ErrNotFound, kind, namespace, name)
	}
	return r, err
}

// ListResources projects all committed resources (used by tests/demo).
func (s *Store) ListResources(ctx context.Context) ([]ResourceRow, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT kind, namespace, name, uid, doc, replicas FROM resources
		 ORDER BY kind, namespace, name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ResourceRow
	for rows.Next() {
		var r ResourceRow
		if err := rows.Scan(&r.Kind, &r.Namespace, &r.Name, &r.UID, &r.Doc, &r.Replicas); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func isUniqueViolation(err error) bool {
	// sqlite3 returns SQLITE_CONSTRAINT (19/2067); cheap string check avoids a
	// driver-type dependency in the contract.
	return err != nil && (containsStr(err.Error(), "UNIQUE constraint") || containsStr(err.Error(), "constraint failed: PRIMARY KEY"))
}

func containsStr(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

// SQLQuota is the real plugins.QuotaService backed by the same database. It
// has two responsibilities at different times:
//
//   - Reserve (during validation) places an idempotent in-flight HOLD. The hold
//     reserves headroom against other concurrent admits; it does NOT change
//     committed usage. The authoritative limit check runs later inside the
//     commit transaction (Store.checkCapacityTx) against the real resources
//     table. That split is what makes UPDATE/DELETE quota accounting exact:
//     committed usage always equals SUM(committed resources.replicas).
//   - Release removes holds of requests that do not reach commit.
//
// Holds are deleted when their request commits (the resource row then
// represents the usage), so quota_ledger never accumulates history.
type SQLQuota struct {
	store  *Store
	limits map[string]int64
}

// NewSQLQuota builds the ledger and installs the limits on the store so the
// commit-time gate enforces them. Kinds absent from limits are unlimited.
func NewSQLQuota(store *Store, limits map[string]int64) *SQLQuota {
	q := &SQLQuota{store: store, limits: limits}
	store.SetQuotaLimits(limits)
	return q
}

// Reserve implements plugins.QuotaService. It inserts a hold row idempotently
// keyed by (uid, kind), so a retried/duplicate request never double-reserves.
// The hold amount is validated against current room to provide an early,
// friendly rejection; the final gate at commit is authoritative.
func (q *SQLQuota) Reserve(ctx context.Context, uid, kind string, amount int64, op model.Operation) error {
	delta := plugins.SignedDelta(amount, op)
	if delta <= 0 {
		return nil
	}
	tx, err := q.store.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	// UID-idempotent: an existing hold for this request means it already
	// reserved; re-entrant reserve is a no-op.
	var existing int
	if err := tx.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM quota_ledger WHERE uid = ? AND kind = ?`, uid, kind).Scan(&existing); err != nil {
		return err
	}
	if existing > 0 {
		return nil
	}

	if limit, limited := q.limits[kind]; limited {
		var committed int64
		if err := tx.QueryRowContext(ctx,
			`SELECT COALESCE(SUM(replicas), 0) FROM resources WHERE kind = ?`, kind).Scan(&committed); err != nil {
			return err
		}
		var otherHolds int64
		if err := tx.QueryRowContext(ctx,
			`SELECT COALESCE(SUM(delta), 0) FROM quota_ledger WHERE kind = ? AND uid <> ?`, kind, uid).
			Scan(&otherHolds); err != nil {
			return err
		}
		if committed+otherHolds+delta > limit {
			return fmt.Errorf("%w: kind=%s committed=%d otherHolds=%d requested=%d limit=%d",
				plugins.ErrQuotaExhausted, kind, committed, otherHolds, delta, limit)
		}
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO quota_ledger (uid, kind, delta, committed) VALUES (?, ?, ?, 0)`,
		uid, kind, delta); err != nil {
		return err
	}
	return tx.Commit()
}

// Release removes the hold(s) of a request that did not commit.
func (q *SQLQuota) Release(ctx context.Context, uid string) error {
	_, err := q.store.db.ExecContext(ctx, `DELETE FROM quota_ledger WHERE uid = ?`, uid)
	return err
}

// Used reports the currently committed replica usage of a kind (observability).
func (q *SQLQuota) Used(ctx context.Context, kind string) (int64, error) {
	var used int64
	err := q.store.db.QueryRowContext(ctx,
		`SELECT COALESCE(SUM(replicas), 0) FROM resources WHERE kind = ?`, kind).
		Scan(&used)
	return used, err
}
