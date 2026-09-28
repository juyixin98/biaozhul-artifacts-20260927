package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/local/evictioncoordinator/internal/domain"
)

// txIsh is satisfied by both *sql.DB and *sql.Tx so reads can run either
// standalone or inside the reservation transaction.
type txIsh interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

// withImmediateTx runs fn inside an explicit BEGIN IMMEDIATE write transaction.
// The write lock is taken up-front, so two concurrent eviction requests
// serialise at the lock (busy_timeout makes the waiter retry) instead of both
// reading the same free slot and deadlocking at COMMIT.
//
// modernc.org/sqlite is driven in autocommit mode on a dedicated connection:
// we issue BEGIN IMMEDIATE / COMMIT ourselves so the lock upgrade happens before
// any read. The callback receives the connection as a txIsh, which exposes the
// same Query/Exec helpers used for standalone reads.
func (s *Store) withImmediateTx(ctx context.Context, fn func(q txIsh) error) error {
	conn, err := s.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return fmt.Errorf("acquire write lock: %w", err)
	}
	if err := fn(conn); err != nil {
		_, _ = conn.ExecContext(ctx, "ROLLBACK")
		return err
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		_, _ = conn.ExecContext(ctx, "ROLLBACK")
		return err
	}
	return nil
}

// --- groups -----------------------------------------------------------------

func labelsToJSON(m map[string]string) string {
	b, _ := json.Marshal(m)
	return string(b)
}

func labelsFromJSON(s string) map[string]string {
	var m map[string]string
	if err := json.Unmarshal([]byte(s), &m); err != nil {
		return map[string]string{}
	}
	return m
}

const groupCols = `namespace, name, replicas, budget_mode, budget_value, budget_percent,
	selector_labels, selector_epoch, created_at, updated_at`

func scanGroup(row interface {
	Scan(dest ...any) error
}) (domain.Group, error) {
	var g domain.Group
	var mode, labels, created, updated string
	err := row.Scan(&g.Namespace, &g.Name, &g.Replicas, &mode, &g.BudgetValue,
		&g.BudgetPercent, &labels, &g.SelectorEpoch, &created, &updated)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return g, ErrNotFound
		}
		return g, err
	}
	g.BudgetMode = domain.BudgetMode(mode)
	g.SelectorLabels = labelsFromJSON(labels)
	g.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	g.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	return g, nil
}

// UpsertGroup creates or replaces a group definition. Replacing the selector
// bumps the epoch (the coordinator decides the new value inside a transaction).
func (s *Store) UpsertGroup(ctx context.Context, g domain.Group, bumpEpoch bool) (domain.Group, error) {
	now := time.Now()
	return s.upsertGroupOn(ctx, s.db, g, bumpEpoch, now)
}

func (s *Store) upsertGroupOn(ctx context.Context, q txIsh, g domain.Group, bumpEpoch bool, now time.Time) (domain.Group, error) {
	existing, err := s.getGroupOn(ctx, q, g.Namespace, g.Name)
	if errors.Is(err, ErrNotFound) {
		if g.SelectorEpoch == 0 {
			g.SelectorEpoch = 1
		}
		g.CreatedAt, g.UpdatedAt = now, now
	} else if err != nil {
		return g, err
	} else {
		g.CreatedAt = existing.CreatedAt
		g.UpdatedAt = now
		if bumpEpoch {
			g.SelectorEpoch = existing.SelectorEpoch + 1
		} else {
			g.SelectorEpoch = existing.SelectorEpoch
		}
	}
	_, err = q.ExecContext(ctx, `
		INSERT INTO groups_meta(namespace,name,replicas,budget_mode,budget_value,
			budget_percent,selector_labels,selector_epoch,created_at,updated_at)
		VALUES(?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(namespace,name) DO UPDATE SET
			replicas=excluded.replicas,
			budget_mode=excluded.budget_mode,
			budget_value=excluded.budget_value,
			budget_percent=excluded.budget_percent,
			selector_labels=excluded.selector_labels,
			selector_epoch=excluded.selector_epoch,
			updated_at=excluded.updated_at`,
		g.Namespace, g.Name, g.Replicas, string(g.BudgetMode), g.BudgetValue,
		g.BudgetPercent, labelsToJSON(g.SelectorLabels), g.SelectorEpoch,
		nowTS(g.CreatedAt), nowTS(g.UpdatedAt))
	return g, err
}

// GetGroup loads one group.
func (s *Store) GetGroup(ctx context.Context, namespace, name string) (domain.Group, error) {
	return s.getGroupOn(ctx, s.db, namespace, name)
}

func (s *Store) getGroupOn(ctx context.Context, q txIsh, namespace, name string) (domain.Group, error) {
	row := q.QueryRowContext(ctx,
		`SELECT `+groupCols+` FROM groups_meta WHERE namespace=? AND name=?`,
		namespace, name)
	return scanGroup(row)
}

// BumpSelectorEpoch advances a group's selector version by one.
func (s *Store) BumpSelectorEpoch(ctx context.Context, namespace, name string) (domain.Group, int64, error) {
	var out domain.Group
	var oldEpoch int64
	err := s.withImmediateTx(ctx, func(tx txIsh) error {
		g, err := s.getGroupOn(ctx, tx, namespace, name)
		if err != nil {
			return err
		}
		oldEpoch = g.SelectorEpoch
		g.SelectorEpoch++
		g.UpdatedAt = time.Now()
		if _, err := tx.ExecContext(ctx,
			`UPDATE groups_meta SET selector_epoch=?, updated_at=? WHERE namespace=? AND name=?`,
			g.SelectorEpoch, nowTS(g.UpdatedAt), namespace, name); err != nil {
			return err
		}
		// Pending approvals from the old epoch cannot be carried over, but their
		// budget slot is NOT silently freed: they are marked stale and await an
		// explicit reclamation confirmation.
		if _, err := tx.ExecContext(ctx,
			`UPDATE approvals SET state=? WHERE namespace=? AND group_name=? AND state=? AND epoch<?`,
			string(domain.ApprovalStale), namespace, name, string(domain.ApprovalPending), g.SelectorEpoch); err != nil {
			return err
		}
		out = g
		return nil
	})
	return out, oldEpoch, err
}

// --- observations & failures ------------------------------------------------

// RecordObservation upserts instance metadata and appends a readiness fact.
func (s *Store) RecordObservation(ctx context.Context, inst domain.Instance, o domain.Observation) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO instances(id,namespace,group_name,labels,updated_at)
		VALUES(?,?,?,?,?)
		ON CONFLICT(id) DO UPDATE SET labels=excluded.labels, updated_at=excluded.updated_at`,
		inst.ID, inst.Namespace, inst.Group, labelsToJSON(inst.Labels), nowTS(o.At))
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO observations(instance_id,ready,epoch,source,at) VALUES(?,?,?,?,?)`,
		o.InstanceID, o.Ready, o.Epoch, o.Source, nowTS(o.At))
	return err
}

// RecordFailure appends an involuntary failure fact. Failures are append-only.
func (s *Store) RecordFailure(ctx context.Context, f domain.Failure) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO failures(instance_id,reason,epoch,source,at) VALUES(?,?,?,?,?)`,
		f.InstanceID, f.Reason, f.Epoch, f.Source, nowTS(f.At))
	return err
}

// MemberInstances returns instances registered against a group (membership is
// re-evaluated against the selector by the coordinator).
func (s *Store) MemberInstances(ctx context.Context, namespace, groupName string) ([]domain.Instance, error) {
	return s.memberInstancesOn(ctx, s.db, namespace, groupName)
}

func (s *Store) memberInstancesOn(ctx context.Context, q txIsh, namespace, groupName string) ([]domain.Instance, error) {
	rows, err := q.QueryContext(ctx,
		`SELECT id, namespace, group_name, labels FROM instances WHERE namespace=? AND group_name=?`,
		namespace, groupName)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.Instance
	for rows.Next() {
		var id, ns, gn, labels string
		if err := rows.Scan(&id, &ns, &gn, &labels); err != nil {
			return nil, err
		}
		out = append(out, domain.Instance{ID: id, Namespace: ns, Group: gn, Labels: labelsFromJSON(labels)})
	}
	return out, rows.Err()
}

// Instance returns one registered instance (used to match a specific eviction
// target against the selector).
func (s *Store) Instance(ctx context.Context, id string) (domain.Instance, error) {
	var inst domain.Instance
	var labels string
	row := s.db.QueryRowContext(ctx,
		`SELECT id, namespace, group_name, labels FROM instances WHERE id=?`, id)
	err := row.Scan(&inst.ID, &inst.Namespace, &inst.Group, &labels)
	if errors.Is(err, sql.ErrNoRows) {
		return inst, ErrNotFound
	}
	inst.Labels = labelsFromJSON(labels)
	return inst, err
}

// LatestObservation returns the newest observation for an instance, plus
// whether one exists at all.
func (s *Store) LatestObservation(ctx context.Context, instanceID string) (domain.Observation, bool, error) {
	return s.latestObservationOn(ctx, s.db, instanceID)
}

func (s *Store) latestObservationOn(ctx context.Context, q txIsh, instanceID string) (domain.Observation, bool, error) {
	var o domain.Observation
	var ready int
	var at string
	row := q.QueryRowContext(ctx,
		`SELECT instance_id, ready, epoch, source, at FROM observations
		 WHERE instance_id=? ORDER BY id DESC LIMIT 1`, instanceID)
	err := row.Scan(&o.InstanceID, &ready, &o.Epoch, &o.Source, &at)
	if errors.Is(err, sql.ErrNoRows) {
		return o, false, nil
	}
	if err != nil {
		return o, false, err
	}
	o.Ready = ready != 0
	o.At, _ = time.Parse(time.RFC3339Nano, at)
	return o, true, nil
}

// HasFailureAtEpoch reports whether an involuntary failure was recorded for the
// instance at or above the given epoch (a failure is a current fact regardless
// of later readiness flapping; only a selector reset clears relevance).
func (s *Store) HasFailureAtEpoch(ctx context.Context, q txIsh, instanceID string, epoch int64) (bool, string, error) {
	var reason, at string
	row := q.QueryRowContext(ctx,
		`SELECT reason, at FROM failures WHERE instance_id=? AND epoch>=?
		 ORDER BY id DESC LIMIT 1`, instanceID, epoch)
	err := row.Scan(&reason, &at)
	if errors.Is(err, sql.ErrNoRows) {
		return false, "", nil
	}
	if err != nil {
		return false, "", err
	}
	return true, reason, nil
}

// --- approvals --------------------------------------------------------------

func scanApproval(row interface{ Scan(dest ...any) error }) (domain.Approval, error) {
	var a domain.Approval
	var state, reserved, expires string
	var reclaimed sql.NullString
	var reason sql.NullString
	err := row.Scan(&a.ID, &a.Namespace, &a.Group, &a.InstanceID, &a.Epoch, &state,
		&reserved, &expires, &reclaimed, &reason)
	if errors.Is(err, sql.ErrNoRows) {
		return a, ErrNotFound
	}
	if err != nil {
		return a, err
	}
	a.State = domain.ApprovalState(state)
	a.ReservedAt, _ = time.Parse(time.RFC3339Nano, reserved)
	a.ExpiresAt, _ = time.Parse(time.RFC3339Nano, expires)
	if reclaimed.Valid {
		t, _ := time.Parse(time.RFC3339Nano, reclaimed.String)
		a.ReclaimedAt = &t
	}
	a.ResultReason = reason.String
	return a, nil
}

const approvalCols = `id, namespace, group_name, instance_id, epoch, state, reserved_at,
	expires_at, reclaimed_at, result_reason`

// PendingApprovalForInstance returns the live (pending) approval reserving a
// slot for the instance, if any.
func (s *Store) PendingApprovalForInstance(ctx context.Context, q txIsh, instanceID string) (domain.Approval, bool, error) {
	row := q.QueryRowContext(ctx,
		`SELECT `+approvalCols+` FROM approvals WHERE instance_id=? AND state=?`,
		instanceID, string(domain.ApprovalPending))
	a, err := scanApproval(row)
	if errors.Is(err, ErrNotFound) {
		return a, false, nil
	}
	return a, err == nil, err
}

// PendingApprovalsForGroup returns all live approvals for a group (the
// coordinator filters epoch membership).
func (s *Store) PendingApprovalsForGroup(ctx context.Context, q txIsh, namespace, groupName string) ([]domain.Approval, error) {
	rows, err := q.QueryContext(ctx,
		`SELECT `+approvalCols+` FROM approvals WHERE namespace=? AND group_name=? AND state=?`,
		namespace, groupName, string(domain.ApprovalPending))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.Approval
	for rows.Next() {
		a, err := scanApproval(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// ChargedApprovalsForGroup returns every approval that still holds a budget
// reservation: pending ones plus expired/stale ones whose slot has not been
// explicitly reclaimed (reclaimed_at IS NULL). succeeded/failed reservations
// were released at completion time and carry a reclaimed_at. This is the list
// the budget engine must count — an expired approval is NOT silently refunded.
func (s *Store) ChargedApprovalsForGroup(ctx context.Context, q txIsh, namespace, groupName string) ([]domain.Approval, error) {
	rows, err := q.QueryContext(ctx,
		`SELECT `+approvalCols+` FROM approvals
		 WHERE namespace=? AND group_name=? AND reclaimed_at IS NULL
		   AND state IN (?,?,?)`,
		namespace, groupName,
		string(domain.ApprovalPending), string(domain.ApprovalExpired), string(domain.ApprovalStale))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.Approval
	for rows.Next() {
		a, err := scanApproval(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// InsertApproval persists a newly granted approval inside the reservation tx.
func (s *Store) InsertApproval(ctx context.Context, q txIsh, a domain.Approval) error {
	var reclaimed any
	if a.ReclaimedAt != nil {
		reclaimed = nowTS(*a.ReclaimedAt)
	}
	_, err := q.ExecContext(ctx, `
		INSERT INTO approvals(id,namespace,group_name,instance_id,epoch,state,
			reserved_at,expires_at,reclaimed_at,result_reason)
		VALUES(?,?,?,?,?,?,?,?,?,?)`,
		a.ID, a.Namespace, a.Group, a.InstanceID, a.Epoch, string(a.State),
		nowTS(a.ReservedAt), nowTS(a.ExpiresAt), reclaimed, a.ResultReason)
	return err
}

// GetApproval loads an approval by id.
func (s *Store) GetApproval(ctx context.Context, id string) (domain.Approval, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT `+approvalCols+` FROM approvals WHERE id=?`, id)
	return scanApproval(row)
}

// SetApprovalResult moves a pending approval to succeeded/failed and frees its
// reservation inside a write transaction.
func (s *Store) SetApprovalResult(ctx context.Context, id string, state domain.ApprovalState, reason string, at time.Time) (domain.Approval, error) {
	var out domain.Approval
	err := s.withImmediateTx(ctx, func(tx txIsh) error {
		a, err := s.getApprovalOn(ctx, tx, id)
		if err != nil {
			return err
		}
		if a.State != domain.ApprovalPending {
			return fmt.Errorf("%w: approval %s is %s, cannot report %s",
				ErrStateConflict, id, a.State, state)
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE approvals SET state=?, result_reason=?, reclaimed_at=? WHERE id=?`,
			string(state), reason, nowTS(at), id); err != nil {
			return err
		}
		a.State = state
		a.ResultReason = reason
		a.ReclaimedAt = &at
		out = a
		return nil
	})
	return out, err
}

// ErrStateConflict signals an approval lifecycle transition was attempted from
// the wrong state.
var ErrStateConflict = errors.New("store: approval state conflict")

func (s *Store) getApprovalOn(ctx context.Context, q txIsh, id string) (domain.Approval, error) {
	row := q.QueryRowContext(ctx,
		`SELECT `+approvalCols+` FROM approvals WHERE id=?`, id)
	return scanApproval(row)
}

// MarkExpired moves pending approvals past their deadline to expired. Like a
// selector change this does NOT release the slot — reclamation is explicit.
// Returns the affected approvals for diagnostics.
func (s *Store) MarkExpired(ctx context.Context, now time.Time) ([]domain.Approval, error) {
	var ids []string
	err := s.withImmediateTx(ctx, func(tx txIsh) error {
		rows, err := tx.QueryContext(ctx,
			`SELECT id FROM approvals WHERE state=? AND expires_at < ?`,
			string(domain.ApprovalPending), nowTS(now))
		if err != nil {
			return err
		}
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		rows.Close()
		for _, id := range ids {
			if _, err := tx.ExecContext(ctx,
				`UPDATE approvals SET state=? WHERE id=? AND state=?`,
				string(domain.ApprovalExpired), id, string(domain.ApprovalPending)); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	out := make([]domain.Approval, 0, len(ids))
	for _, id := range ids {
		a, err := s.GetApproval(ctx, id)
		if err == nil {
			out = append(out, a)
		}
	}
	return out, nil
}

// ReclaimApproval releases the reservation held by an expired/stale approval.
// It requires an explicit confirmation token equal to the approval id (the
// caller must first GET the approval, see its reclaimable state, and confirm
// they mean to return that exact slot). This prevents silent budget refunds.
func (s *Store) ReclaimApproval(ctx context.Context, id, confirmation string, at time.Time) (domain.Approval, error) {
	if confirmation == "" || confirmation != id {
		return domain.Approval{}, ErrConfirmationRequired
	}
	var out domain.Approval
	err := s.withImmediateTx(ctx, func(tx txIsh) error {
		a, err := s.getApprovalOn(ctx, tx, id)
		if err != nil {
			return err
		}
		if !a.State.Reclaimable() {
			return fmt.Errorf("%w: approval %s is %s, only expired/stale may be reclaimed",
				ErrStateConflict, id, a.State)
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE approvals SET reclaimed_at=?, result_reason=COALESCE(NULLIF(result_reason,''),'reclaimed') WHERE id=?`,
			nowTS(at), id); err != nil {
			return err
		}
		// A reclaimed approval keeps its expired/stale terminal state; the
		// non-null reclaimed_at is what removes it from any future budget view.
		a.ReclaimedAt = &at
		out = a
		return nil
	})
	return out, err
}

// ErrConfirmationRequired is returned when reclaim is called without the
// explicit confirmation token.
var ErrConfirmationRequired = errors.New("store: reclaim confirmation required")

// InsertDecision appends a structured decision record.
func (s *Store) InsertDecision(ctx context.Context, q txIsh, d domain.Decision, instanceID string, at time.Time) error {
	snapJSON, _ := json.Marshal(d.Snapshot)
	accepted := 0
	if d.Accepted {
		accepted = 1
	}
	_, err := q.ExecContext(ctx, `
		INSERT INTO decisions_log(request_id,namespace,group_name,instance_id,
			accepted,category,reason,approval_id,snapshot,at)
		VALUES(?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(request_id) DO NOTHING`,
		d.RequestID, namespaceOf(d.Group), nameOf(d.Group), instanceID,
		accepted, string(d.Category), d.Reason, d.ApprovalID, string(snapJSON), nowTS(at))
	return err
}

// GetDecisionOn returns a previously recorded decision for a request id,
// enabling honest idempotent replay inside a transaction.
func (s *Store) GetDecisionOn(ctx context.Context, q txIsh, requestID string) (domain.Decision, bool, error) {
	var accepted int
	var category, reason, approvalID, snapJSON, namespace, groupName, instanceID string
	row := q.QueryRowContext(ctx,
		`SELECT namespace, group_name, instance_id, accepted, category, reason, approval_id, snapshot
		 FROM decisions_log WHERE request_id=?`, requestID)
	err := row.Scan(&namespace, &groupName, &instanceID, &accepted, &category, &reason, &approvalID, &snapJSON)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.Decision{}, false, nil
	}
	if err != nil {
		return domain.Decision{}, false, err
	}
	var snap domain.BudgetSnapshot
	_ = json.Unmarshal([]byte(snapJSON), &snap)
	return domain.Decision{
		RequestID:  requestID,
		Group:      namespace + "/" + groupName,
		Instance:   instanceID,
		Accepted:   accepted != 0,
		Category:   domain.FailureCategory(category),
		Reason:     reason,
		ApprovalID: approvalID,
		Snapshot:   snap,
	}, true, nil
}

func namespaceOf(key string) string {
	for i := 0; i < len(key); i++ {
		if key[i] == '/' {
			return key[:i]
		}
	}
	return ""
}

func nameOf(key string) string {
	for i := 0; i < len(key); i++ {
		if key[i] == '/' {
			return key[i+1:]
		}
	}
	return key
}

// ListReclaimable returns approvals currently awaiting explicit reclamation.
func (s *Store) ListReclaimable(ctx context.Context, namespace, groupName string) ([]domain.Approval, error) {
	q := `SELECT ` + approvalCols + ` FROM approvals WHERE reclaimed_at IS NULL AND state IN (?, ?)`
	args := []any{string(domain.ApprovalExpired), string(domain.ApprovalStale)}
	if groupName != "" {
		q += ` AND namespace=? AND group_name=?`
		args = append(args, namespace, groupName)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.Approval
	for rows.Next() {
		a, err := scanApproval(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
