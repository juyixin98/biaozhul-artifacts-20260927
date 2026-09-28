package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"pvsim/engine"
	"pvsim/model"
)

// RunRecord is the row representation of a completed/attempted run.
type RunRecord struct {
	RunID             string `json:"run_id"`
	Name              string `json:"name"`
	Status            string `json:"status"`
	Converged         bool   `json:"converged"`
	NonConvergentCode string `json:"non_convergent_code,omitempty"`
	Steps             int    `json:"steps"`
	Versions          int    `json:"versions"`
	CycleJSON         string `json:"-"`
	ScenarioJSON      string `json:"-"`
	ResultJSON        string `json:"-"`
	ErrorKind         string `json:"error_kind,omitempty"`
	ErrorCode         string `json:"error_code,omitempty"`
	ErrorMessage      string `json:"error_message,omitempty"`
	CreatedAt         string `json:"created_at,omitempty"`
}

// DecisionRow is one persisted best-path decision.
type DecisionRow struct {
	Version    int    `json:"version"`
	Router     string `json:"router"`
	Prefix     string `json:"prefix"`
	PrevPeer   string `json:"previous_peer"`
	ChosenPeer string `json:"chosen_peer"`
	RunnerUp   string `json:"runner_up"`
	Reason     string `json:"reason"`
	AttrsJSON  string `json:"-"`
}

// TraceRow is one persisted trace line.
type TraceRow struct {
	Version         int    `json:"version"`
	Category        string `json:"category"`
	Router          string `json:"router"`
	Peer            string `json:"peer"`
	Prefix          string `json:"prefix"`
	Detail          string `json:"detail"`
	AttrsBeforeJSON string `json:"-"`
	AttrsAfterJSON  string `json:"-"`
}

// DeliveryRow is one external event delivery.
type DeliveryRow struct {
	Version int    `json:"version"`
	Seq     int    `json:"seq"`
	Router  string `json:"router"`
	Peer    string `json:"peer"`
	Kind    string `json:"kind"`
	Prefix  string `json:"prefix"`
}

// SaveParams carries everything needed to persist one run transactionally.
type SaveParams struct {
	Record    RunRecord
	Collector *engine.Collector
}

// SaveRun writes a run and all its artifacts in one transaction.
// Returns ErrExists when RunID is already taken.
func (s *Store) SaveRun(ctx context.Context, p SaveParams) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return mapDBErr(err)
	}
	defer tx.Rollback()

	var exists int
	if err := tx.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM runs WHERE run_id = ?`, p.Record.RunID).Scan(&exists); err != nil {
		return mapDBErr(err)
	}
	if exists > 0 {
		return ErrExists
	}
	r := p.Record
	conv := 0
	if r.Converged {
		conv = 1
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO runs
		(run_id, name, status, converged, non_convergent_code, steps, versions,
		 cycle_json, scenario_json, result_json, error_kind, error_code, error_message)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		r.RunID, r.Name, r.Status, conv, r.NonConvergentCode, r.Steps, r.Versions,
		r.CycleJSON, r.ScenarioJSON, r.ResultJSON, r.ErrorKind, r.ErrorCode, r.ErrorMessage); err != nil {
		return mapDBErr(err)
	}
	if p.Collector != nil {
		if err := saveArtifacts(ctx, tx, r.RunID, p.Collector); err != nil {
			return err
		}
	}
	return mapDBErr(tx.Commit())
}

func saveArtifacts(ctx context.Context, tx *sql.Tx, runID string, c *engine.Collector) error {
	for _, d := range c.Events {
		if _, err := tx.ExecContext(ctx, `INSERT INTO deliveries
			(run_id, version, seq, router, peer, kind, prefix) VALUES (?,?,?,?,?,?,?)`,
			runID, d.Version, d.Seq, d.Router, d.Peer, d.Kind, d.Prefix); err != nil {
			return mapDBErr(err)
		}
	}
	for _, d := range c.Decisions {
		attrs, _ := json.Marshal(d.ChosenAttrs)
		if _, err := tx.ExecContext(ctx, `INSERT INTO decisions
			(run_id, version, router, prefix, prev_peer, chosen_peer, runner_up, reason, attrs_json)
			VALUES (?,?,?,?,?,?,?,?,?)`,
			runID, d.Version, d.Router, d.Prefix, d.PreviousPeer, d.ChosenPeer,
			d.RunnerUpPeer, d.Reason, string(attrs)); err != nil {
			return mapDBErr(err)
		}
	}
	for _, t := range c.Traces {
		var before, after string
		if t.AttrsBefore != nil {
			b, _ := json.Marshal(t.AttrsBefore)
			before = string(b)
		}
		if t.AttrsAfter != nil {
			b, _ := json.Marshal(t.AttrsAfter)
			after = string(b)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO traces
			(run_id, version, category, router, peer, prefix, detail, attrs_before_json, attrs_after_json)
			VALUES (?,?,?,?,?,?,?,?,?)`,
			runID, t.Version, t.Category, t.Router, t.Peer, t.Prefix, t.Detail, before, after); err != nil {
			return mapDBErr(err)
		}
	}
	return nil
}

// GetRun loads one run header.
func (s *Store) GetRun(ctx context.Context, runID string) (RunRecord, error) {
	row := s.db.QueryRowContext(ctx, `SELECT
		run_id, name, status, converged, non_convergent_code, steps, versions,
		cycle_json, scenario_json, result_json, error_kind, error_code, error_message, created_at
		FROM runs WHERE run_id = ?`, runID)
	var r RunRecord
	var conv int
	if err := row.Scan(&r.RunID, &r.Name, &r.Status, &conv, &r.NonConvergentCode,
		&r.Steps, &r.Versions, &r.CycleJSON, &r.ScenarioJSON, &r.ResultJSON,
		&r.ErrorKind, &r.ErrorCode, &r.ErrorMessage, &r.CreatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return RunRecord{}, ErrNotFound
		}
		return RunRecord{}, mapDBErr(err)
	}
	r.Converged = conv == 1
	return r, nil
}

// RunResultJSON returns the stored result payload of a run.
func (s *Store) RunResultJSON(ctx context.Context, runID string) ([]byte, error) {
	r, err := s.GetRun(ctx, runID)
	if err != nil {
		return nil, err
	}
	return []byte(r.ResultJSON), nil
}

// RunScenarioJSON returns the exact scenario bytes that were replayed.
func (s *Store) RunScenarioJSON(ctx context.Context, runID string) ([]byte, error) {
	r, err := s.GetRun(ctx, runID)
	if err != nil {
		return nil, err
	}
	return []byte(r.ScenarioJSON), nil
}

// runExists returns ErrNotFound when no such run exists.
func (s *Store) runExists(ctx context.Context, runID string) error {
	var n int
	if err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM runs WHERE run_id = ?`, runID).Scan(&n); err != nil {
		return mapDBErr(err)
	}
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

// GetDeliveries returns external deliveries in version order.
func (s *Store) GetDeliveries(ctx context.Context, runID string) ([]DeliveryRow, error) {
	if err := s.runExists(ctx, runID); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT version, seq, router, peer, kind, prefix
		FROM deliveries WHERE run_id = ? ORDER BY version`, runID)
	if err != nil {
		return nil, mapDBErr(err)
	}
	defer rows.Close()
	return scanDeliveries(rows)
}

// GetDecisions returns decisions in id order.
func (s *Store) GetDecisions(ctx context.Context, runID string) ([]DecisionRow, error) {
	if err := s.runExists(ctx, runID); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT version, router, prefix, prev_peer,
		chosen_peer, runner_up, reason, attrs_json FROM decisions
		WHERE run_id = ? ORDER BY id`, runID)
	if err != nil {
		return nil, mapDBErr(err)
	}
	defer rows.Close()
	var out []DecisionRow
	for rows.Next() {
		var d DecisionRow
		if err := rows.Scan(&d.Version, &d.Router, &d.Prefix, &d.PrevPeer,
			&d.ChosenPeer, &d.RunnerUp, &d.Reason, &d.AttrsJSON); err != nil {
			return nil, mapDBErr(err)
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// GetTraces returns traces in id order.
func (s *Store) GetTraces(ctx context.Context, runID string) ([]TraceRow, error) {
	if err := s.runExists(ctx, runID); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT version, category, router, peer,
		prefix, detail, attrs_before_json, attrs_after_json FROM traces
		WHERE run_id = ? ORDER BY id`, runID)
	if err != nil {
		return nil, mapDBErr(err)
	}
	defer rows.Close()
	var out []TraceRow
	for rows.Next() {
		var t TraceRow
		if err := rows.Scan(&t.Version, &t.Category, &t.Router, &t.Peer,
			&t.Prefix, &t.Detail, &t.AttrsBeforeJSON, &t.AttrsAfterJSON); err != nil {
			return nil, mapDBErr(err)
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// ListRuns lists run headers, newest first.
func (s *Store) ListRuns(ctx context.Context, limit int) ([]RunRecord, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `SELECT
		run_id, name, status, converged, non_convergent_code, steps, versions,
		'', '', '', error_kind, error_code, error_message, created_at
		FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, mapDBErr(err)
	}
	defer rows.Close()
	var out []RunRecord
	for rows.Next() {
		var r RunRecord
		var conv int
		if err := rows.Scan(&r.RunID, &r.Name, &r.Status, &conv, &r.NonConvergentCode,
			&r.Steps, &r.Versions, &r.CycleJSON, &r.ScenarioJSON, &r.ResultJSON,
			&r.ErrorKind, &r.ErrorCode, &r.ErrorMessage, &r.CreatedAt); err != nil {
			return nil, mapDBErr(err)
		}
		r.Converged = conv == 1
		out = append(out, r)
	}
	return out, rows.Err()
}

func scanDeliveries(rows *sql.Rows) ([]DeliveryRow, error) {
	var out []DeliveryRow
	for rows.Next() {
		var d DeliveryRow
		if err := rows.Scan(&d.Version, &d.Seq, &d.Router, &d.Peer, &d.Kind, &d.Prefix); err != nil {
			return nil, mapDBErr(err)
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

func mapDBErr(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, sql.ErrNoRows) {
		return ErrNotFound
	}
	msg := err.Error()
	// SQLite resource/space failures surface via distinct error strings in
	// the pure-Go driver; keep the category explicit rather than guessing
	// upstream.
	if containsAny(msg, "disk full", "database or disk is full", "too many", "SQLITE_FULL") {
		return model.NewError(model.KindResourceExhausted, "STORAGE_EXHAUSTED", "%v", err)
	}
	return fmt.Errorf("sqlite: %w", err)
}

func containsAny(s string, subs ...string) bool {
	for _, x := range subs {
		if len(x) > 0 && indexOf(s, x) >= 0 {
			return true
		}
	}
	return false
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
