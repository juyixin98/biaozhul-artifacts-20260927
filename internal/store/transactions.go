package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"evictor/internal/domain"
)

// ObservedInstance is one element of an ingested readiness observation.
type ObservedInstance struct {
	ID         string
	State      string
	Labels     map[string]string
	SelVersion int64
}

// IngestResult reports the state transitions the observation revealed. The
// coordinator turns these into reclaim events / diagnostics outside the
// raw SQL but still inside one atomic ingestion transaction (IngestObservation).
type IngestResult struct {
	ObservationID int64
	// Failures: instances that are failed in THIS observation.
	Failures []string
	// Gone: instances observed gone in this observation.
	Gone []string
	// Draining: instances observed draining.
	Draining []string
	// Ready: instances observed ready.
	Ready []string
}

// IngestObservation atomically persists one group observation: the header,
// per-instance rows, topology upserts and the instances' last-observed times.
// Returned transitions let the coordinator settle open evictions without a
// second (racy) read.
func (s *Store) IngestObservation(ctx context.Context, group string, at time.Time, rows []ObservedInstance) (IngestResult, error) {
	var res IngestResult
	err := s.WithTx(ctx, func(q DBTX) error {
		r, err := q.ExecContext(ctx,
			`INSERT INTO observations(group_name, observed_at) VALUES(?, ?)`,
			group, at.UnixMilli())
		if err != nil {
			return err
		}
		id, err := r.LastInsertId()
		if err != nil {
			return err
		}
		res.ObservationID = id
		for _, o := range rows {
			if !domain.InstanceState(o.State).Valid() {
				return fmt.Errorf("store: invalid instance state %q", o.State)
			}
			labels := domain.CanonicalLabels(o.Labels)
			if _, err := q.ExecContext(ctx, `
INSERT INTO instances(instance_id, group_name, labels, selector_version, last_observed_at)
VALUES(?, ?, ?, ?, ?)
ON CONFLICT(instance_id) DO UPDATE SET
	last_observed_at=excluded.last_observed_at,
	selector_version=CASE WHEN excluded.selector_version > 0
		THEN excluded.selector_version ELSE instances.selector_version END,
	labels=CASE WHEN ? <> '' THEN ? ELSE instances.labels END`,
				o.ID, group, labels, o.SelVersion, at.UnixMilli(), labels, labels); err != nil {
				return err
			}
			if _, err := q.ExecContext(ctx, `
INSERT INTO observation_rows(observation_id, instance_id, state) VALUES(?, ?, ?)`,
				id, o.ID, o.State); err != nil {
				return err
			}
			switch domain.InstanceState(o.State) {
			case domain.StateFailed:
				res.Failures = append(res.Failures, o.ID)
			case domain.StateGone:
				res.Gone = append(res.Gone, o.ID)
			case domain.StateDraining:
				res.Draining = append(res.Draining, o.ID)
			case domain.StateReady:
				res.Ready = append(res.Ready, o.ID)
			}
		}
		return nil
	})
	return res, err
}

// LatestObservationAt returns the timestamp of the newest observation for a
// group (zero time when none exists).
func (s *Store) LatestObservationAt(ctx context.Context, group string) (time.Time, error) {
	var ms sql.NullInt64
	err := s.WithTx(ctx, func(q DBTX) error {
		return q.QueryRowContext(ctx,
			`SELECT MAX(observed_at) FROM observations WHERE group_name = ?`, group).Scan(&ms)
	})
	if err != nil {
		return time.Time{}, err
	}
	if !ms.Valid {
		return time.Time{}, nil
	}
	return time.UnixMilli(ms.Int64), nil
}

// GroupSnapshot is everything the coordinator needs to evaluate a budget and
// admit one request, loaded inside a single transaction.
type GroupSnapshot struct {
	Policy           PolicyRow
	Selector         SelectorRow
	LatestObservedAt time.Time
	Instances        []InstanceRow
}

// LoadSnapshot reads policy + active selector + the LATEST per-instance
// observed state for a group. Instances never observed are included with an
// empty state (treated as "no evidence" -> a fresh observation is required).
func (s *Store) LoadSnapshot(ctx context.Context, group string) (GroupSnapshot, error) {
	var snap GroupSnapshot
	err := s.WithTx(ctx, func(q DBTX) error {
		var err error
		snap.Policy, err = getPolicy(q, ctx, group)
		if err != nil {
			return err
		}
		snap.Selector, err = currentSelector(q, ctx, group)
		if err != nil {
			return err
		}
		var latestMs *int64
		if err := q.QueryRowContext(ctx,
			`SELECT MAX(observed_at) FROM observations WHERE group_name = ?`, group).Scan(&latestMs); err != nil {
			return err
		}
		if latestMs != nil {
			snap.LatestObservedAt = time.UnixMilli(*latestMs)
		}
		rows, err := q.QueryContext(ctx, `
SELECT i.instance_id, i.group_name, i.labels, i.selector_version,
       i.last_observed_at,
       COALESCE((
           SELECT orw.state
           FROM observation_rows orw
           JOIN observations o ON o.id = orw.observation_id
           WHERE orw.instance_id = i.instance_id
           ORDER BY o.observed_at DESC, o.id DESC
           LIMIT 1
       ), '') AS state
FROM instances i
WHERE i.group_name = ?`, group)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var in InstanceRow
			var lastObs int64
			if err := rows.Scan(&in.ID, &in.Group, &in.Labels, &in.SelVersion,
				&lastObs, &in.State); err != nil {
				return err
			}
			if lastObs > 0 {
				in.LastObservedAt = time.UnixMilli(lastObs)
			}
			snap.Instances = append(snap.Instances, in)
		}
		return rows.Err()
	})
	return snap, err
}

// ApprovedEvictions returns all open evictions of a group with their target.
func (s *Store) ApprovedEvictions(ctx context.Context, group string) ([]EvictionRow, error) {
	var out []EvictionRow
	err := s.WithTx(ctx, func(q DBTX) error {
		rows, err := q.QueryContext(ctx, `
SELECT id, group_name, instance_id, phase, outcome_reason, detail,
       selector_version, created_at, COALESCE(approved_at,0), COALESCE(expires_at,0),
       COALESCE(terminal_at,0), COALESCE(observation_id,0)
FROM evictions WHERE group_name = ? AND phase = 'approved'`, group)
		if err != nil {
			return err
		}
		defer rows.Close()
		var e error
		if out, e = scanEvictionRows(rows); e != nil {
			return e
		}
		return nil
	})
	return out, err
}

func scanEvictionRows(rows *sql.Rows) ([]EvictionRow, error) {
	var out []EvictionRow
	for rows.Next() {
		e, err := scanEviction(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// Eviction fetches one eviction by id.
func (s *Store) Eviction(ctx context.Context, id string) (EvictionRow, error) {
	var e EvictionRow
	err := s.WithTx(ctx, func(q DBTX) error {
		row := q.QueryRowContext(ctx, `
SELECT id, group_name, instance_id, phase, outcome_reason, detail,
       selector_version, created_at, COALESCE(approved_at,0), COALESCE(expires_at,0),
       COALESCE(terminal_at,0), COALESCE(observation_id,0)
FROM evictions WHERE id = ?`, id)
		var err error
		e, err = scanEviction(row)
		return err
	})
	return e, err
}

func scanEviction(r rowScanner) (EvictionRow, error) {
	var e EvictionRow
	var created, approved, expires, terminal int64
	if err := r.Scan(&e.ID, &e.Group, &e.InstanceID, &e.Phase,
		&e.OutcomeReason, &e.Detail, &e.SelectorVersion,
		&created, &approved, &expires, &terminal, &e.ObservationID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return EvictionRow{}, ErrNotFound
		}
		return EvictionRow{}, err
	}
	e.CreatedAt = time.UnixMilli(created)
	if approved > 0 {
		e.ApprovedAt = time.UnixMilli(approved)
	}
	if expires > 0 {
		e.ExpiresAt = time.UnixMilli(expires)
	}
	if terminal > 0 {
		e.TerminalAt = time.UnixMilli(terminal)
	}
	return e, nil
}

// ApproveInput carries the caller's pre-computed decision into the atomic
// approve transaction. The transaction RE-CHECKS budget against state it reads
// itself; approving is never allowed to rest purely on a stale caller read.
type ApproveInput struct {
	ID              string
	Group           string
	InstanceID      string
	SelectorVersion int64
	Now             time.Time
	ExpiresAt       time.Time
	Reason          string
	Detail          string
}

// Approve atomically inserts an open eviction AND its budget reservation.
// Because admission re-evaluation and the insert happen in one immediate
// transaction against freshly read rows, two concurrent requests for the
// same group serialize: the loser recomputes budget with the winner's row
// present and is rejected budget-exhausted (or hits the unique open-per
// -instance index for the same target).
//
// The re-check function is injected so the store does not depend on the
// coordinator, while the coordinator does not depend on SQL: evaluate is
// called with rows read INSIDE this tx.
func (s *Store) Approve(ctx context.Context, in ApproveInput,
	evaluate func(q DBTX, now time.Time) error) (EvictionRow, error) {
	var out EvictionRow
	err := s.WithTx(ctx, func(q DBTX) error {
		if err := evaluate(q, in.Now); err != nil {
			return err
		}
		_, err := q.ExecContext(ctx, `
INSERT INTO evictions(id, group_name, instance_id, phase, detail,
                      selector_version, created_at, approved_at, expires_at)
VALUES(?, ?, ?, 'approved', ?, ?, ?, ?, ?)`,
			in.ID, in.Group, in.InstanceID, in.Detail, in.SelectorVersion,
			in.Now.UnixMilli(), in.Now.UnixMilli(), in.ExpiresAt.UnixMilli())
		if err != nil {
			if isUniqueOpen(err) {
				return fmt.Errorf("%w: instance %s already has an open eviction", ErrAlreadyOpen, in.InstanceID)
			}
			return err
		}
		out = EvictionRow{
			ID: in.ID, Group: in.Group, InstanceID: in.InstanceID,
			Phase: string(domain.PhaseApproved), Detail: in.Detail,
			SelectorVersion: in.SelectorVersion,
			CreatedAt: in.Now, ApprovedAt: in.Now, ExpiresAt: in.ExpiresAt,
		}
		return nil
	})
	return out, err
}

// ErrAlreadyOpen signals the database unique index blocked a second open
// eviction for one instance.
var ErrAlreadyOpen = errors.New("store: eviction already open for instance")

func isUniqueOpen(err error) bool {
	if err == nil {
		return false
	}
	m := err.Error()
	return strings.Contains(m, "UNIQUE constraint failed") ||
		strings.Contains(m, "unique constraint failed")
}

// Complete marks an open eviction completed and writes its reclaim event in
// the SAME transaction. A completed approval stops reserving budget exactly
// when the confirmation lands — there is no window where the slot is both
// freed and unaccounted.
func (s *Store) Complete(ctx context.Context, id, detail, confirmedBy string, observationID int64, at time.Time) (ReclaimRow, error) {
	return s.terminate(ctx, id, domain.PhaseCompleted, "completion",
		domain.ReasonAccepted, detail, confirmedBy, observationID, at)
}

// Fail marks an open eviction failed through no budget decision and writes
// the involuntary-failure reclaim event. The failed approval frees its
// reservation immediately and explicitly; the observation's failed replica
// occupies an unavailable slot on its own.
func (s *Store) Fail(ctx context.Context, id, detail, confirmedBy string, observationID int64, at time.Time) (ReclaimRow, error) {
	return s.terminate(ctx, id, domain.PhaseFailed, "involuntary-failure",
		domain.ReasonInvoluntaryFailure, detail, confirmedBy, observationID, at)
}

// Expire reaps an approval whose TTL elapsed. The reclaim event's kind
// "ttl-expiry" is the explicit confirmation artifact that budget was released.
func (s *Store) Expire(ctx context.Context, id, detail, confirmedBy string, at time.Time) (ReclaimRow, error) {
	return s.terminate(ctx, id, domain.PhaseExpired, "ttl-expiry",
		"rejected:approval-ttl-expired", detail, confirmedBy, 0, at)
}

// Revoke invalidates an approval because its selector version was superseded.
func (s *Store) Revoke(ctx context.Context, id, detail, confirmedBy string, at time.Time) (ReclaimRow, error) {
	return s.terminate(ctx, id, domain.PhaseRevoked, "selector-change",
		"rejected:selector-version-superseded", detail, confirmedBy, 0, at)
}

func (s *Store) terminate(ctx context.Context, id string, phase domain.EvictionPhase,
	kind, outcomeReason, detail, confirmedBy string, observationID int64, at time.Time) (ReclaimRow, error) {
	var rc ReclaimRow
	err := s.WithTx(ctx, func(q DBTX) error {
		var e EvictionRow
		row := q.QueryRowContext(ctx, `
SELECT id, group_name, instance_id, phase, outcome_reason, detail,
       selector_version, created_at, COALESCE(approved_at,0), COALESCE(expires_at,0),
       COALESCE(terminal_at,0), COALESCE(observation_id,0)
FROM evictions WHERE id = ?`, id)
		var err error
		e, err = scanEviction(row)
		if err != nil {
			return err
		}
		if e.Phase != string(domain.PhaseApproved) {
			return fmt.Errorf("%w: eviction %s is %s", ErrTerminal, id, e.Phase)
		}
		var obsArg any
		if observationID > 0 {
			obsArg = observationID
		} else {
			obsArg = nil
		}
		if _, err := q.ExecContext(ctx, `
UPDATE evictions SET phase = ?, outcome_reason = ?, detail = ?,
                     terminal_at = ?, observation_id = COALESCE(?, observation_id)
WHERE id = ? AND phase = 'approved'`,
			string(phase), outcomeReason, detail, at.UnixMilli(), obsArg, id); err != nil {
			return err
		}
		r, err := q.ExecContext(ctx, `
INSERT INTO reclaim_events(eviction_id, kind, detail, created_at, confirmed_at, confirmed_by)
VALUES(?, ?, ?, ?, ?, ?)`,
			id, kind, detail, at.UnixMilli(), at.UnixMilli(), confirmedBy)
		if err != nil {
			return err
		}
		rid, _ := r.LastInsertId()
		rc = ReclaimRow{
			ID: rid, EvictionID: id, Kind: kind, Detail: detail,
			CreatedAt: at, ConfirmedAt: at, ConfirmedBy: confirmedBy,
		}
		return nil
	})
	return rc, err
}

// ErrTerminal is returned when an operation needs an open eviction but the
// row is already terminal.
var ErrTerminal = errors.New("store: eviction already terminal")

// ExpiredEvictions lists open evictions whose deadline passed at `at`.
func (s *Store) ExpiredEvictions(ctx context.Context, at time.Time) ([]EvictionRow, error) {
	var out []EvictionRow
	err := s.WithTx(ctx, func(q DBTX) error {
		rows, err := q.QueryContext(ctx, `
SELECT id, group_name, instance_id, phase, outcome_reason, detail,
       selector_version, created_at, COALESCE(approved_at,0), COALESCE(expires_at,0),
       COALESCE(terminal_at,0), COALESCE(observation_id,0)
FROM evictions
WHERE phase = 'approved' AND expires_at IS NOT NULL AND expires_at < ?`, at.UnixMilli())
		if err != nil {
			return err
		}
		defer rows.Close()
		out, err = scanEvictionRows(rows)
		return err
	})
	return out, err
}

// OpenEvictionsForVersions lists open evictions pinned to selector versions
// other than currentVersion (used when a selector change is published).
func (s *Store) OpenEvictionsForVersions(ctx context.Context, group string, currentVersion int64) ([]EvictionRow, error) {
	var out []EvictionRow
	err := s.WithTx(ctx, func(q DBTX) error {
		rows, err := q.QueryContext(ctx, `
SELECT id, group_name, instance_id, phase, outcome_reason, detail,
       selector_version, created_at, COALESCE(approved_at,0), COALESCE(expires_at,0),
       COALESCE(terminal_at,0), COALESCE(observation_id,0)
FROM evictions
WHERE group_name = ? AND phase = 'approved' AND selector_version <> ?`, group, currentVersion)
		if err != nil {
			return err
		}
		defer rows.Close()
		out, err = scanEvictionRows(rows)
		return err
	})
	return out, err
}

// ReclaimEvents returns confirmation records for an eviction (usually 0 or 1).
func (s *Store) ReclaimEvents(ctx context.Context, evictionID string) ([]ReclaimRow, error) {
	var out []ReclaimRow
	err := s.WithTx(ctx, func(q DBTX) error {
		r2, err := q.QueryContext(ctx, `
SELECT id, eviction_id, kind, detail, created_at, confirmed_at, confirmed_by
FROM reclaim_events WHERE eviction_id = ? ORDER BY id`, evictionID)
		if err != nil {
			return err
		}
		defer r2.Close()
		for r2.Next() {
			var rc ReclaimRow
			var created, confirmed int64
			if err := r2.Scan(&rc.ID, &rc.EvictionID, &rc.Kind, &rc.Detail,
				&created, &confirmed, &rc.ConfirmedBy); err != nil {
				return err
			}
			rc.CreatedAt = time.UnixMilli(created)
			rc.ConfirmedAt = time.UnixMilli(confirmed)
			out = append(out, rc)
		}
		return r2.Err()
	})
	return out, err
}
