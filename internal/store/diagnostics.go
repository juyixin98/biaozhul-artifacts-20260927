package store

import (
	"context"
	"time"
)

// DiagnosticRecord is one accept/reject/undecidable audit row. It carries the
// request id and the evaluated budget snapshot so operators can answer "why
// did this request get this answer" from persisted state alone. Sensitive
// payloads are redacted by the caller before they reach Detail.
type DiagnosticRecord struct {
	TS         time.Time
	RequestID  string
	Group      string
	InstanceID string
	Action     string
	Outcome    string // accepted | rejected | undecidable | settled | reaped | revoked
	Reason     string
	Detail     string
	BudgetJSON string
}

func (s *Store) InsertDiagnostic(ctx context.Context, r DiagnosticRecord) error {
	return s.WithTx(ctx, func(q DBTX) error {
		_, err := q.ExecContext(ctx, `
INSERT INTO diagnostics(ts, request_id, group_name, instance_id, action, outcome, reason, detail, budget_json)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			r.TS.UnixMilli(), r.RequestID, r.Group, r.InstanceID,
			r.Action, r.Outcome, r.Reason, r.Detail, r.BudgetJSON)
		return err
	})
}

// CountPhase returns how many evictions of a group are in a phase. Used by
// integration tests to assert exact reservation accounting.
func (s *Store) CountPhase(ctx context.Context, group, phase string) (int, error) {
	var n int
	err := s.WithTx(ctx, func(q DBTX) error {
		return q.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM evictions WHERE group_name = ? AND phase = ?`,
			group, phase).Scan(&n)
	})
	return n, err
}

// CountReclaims returns the number of reclaim confirmation events of a kind.
func (s *Store) CountReclaims(ctx context.Context, kind string) (int, error) {
	var n int
	err := s.WithTx(ctx, func(q DBTX) error {
		return q.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM reclaim_events WHERE kind = ?`, kind).Scan(&n)
	})
	return n, err
}
