package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"replicactl/core/controller"
)

// DecisionLog implements controller.DecisionStore against SQLite.
type DecisionLog struct {
	DB *sql.DB
	// Clock is overridable for deterministic tests.
	Clock func() int64
}

// NewDecisionLog builds the decision store.
func NewDecisionLog(db *sql.DB) *DecisionLog {
	return &DecisionLog{DB: db, Clock: func() int64 { return time.Now().Unix() }}
}

// AppendDecision persists one fully-explained decision row and returns it with
// its assigned ID.
func (l *DecisionLog) AppendDecision(d controller.Decision) (controller.Decision, error) {
	reasons, err := json.Marshal(d.Reasons)
	if err != nil {
		return d, fmt.Errorf("marshal reasons: %w", err)
	}
	obsJSON := ""
	if d.Observation != nil {
		b, err := json.Marshal(d.Observation)
		if err != nil {
			return d, fmt.Errorf("marshal observation: %w", err)
		}
		obsJSON = string(b)
	}
	res, err := l.DB.Exec(`
		INSERT INTO decisions(
			request_id, tick_at, action, current_replicas, desired_replicas,
			reasons_json, failure_class, failure_detail, observation_json, created_at)
		VALUES (?,?,?,?,?,?,?,?,?,?)`,
		d.RequestID, d.TickAt, string(d.Action), d.CurrentReplicas, d.DesiredReplicas,
		string(reasons), string(d.FailureClass), d.FailureDetail, obsJSON, l.Clock())
	if err != nil {
		return d, fmt.Errorf("insert decision: %w", err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		return d, fmt.Errorf("decision id: %w", err)
	}
	d.ID = id
	return d, nil
}

// Recent returns the most recent n decision rows (newest last).
func (l *DecisionLog) Recent(n int) ([]controller.Decision, error) {
	rows, err := l.DB.Query(`
		SELECT id, request_id, tick_at, action, current_replicas, desired_replicas,
		       reasons_json, failure_class, failure_detail, observation_json
		FROM decisions ORDER BY id DESC LIMIT ?`, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var rev []controller.Decision
	for rows.Next() {
		d, err := scanDecision(rows)
		if err != nil {
			return nil, err
		}
		rev = append(rev, d)
	}
	out := make([]controller.Decision, 0, len(rev))
	for i := len(rev) - 1; i >= 0; i-- {
		out = append(out, rev[i])
	}
	return out, rows.Err()
}

// ByRequestID returns the decision row carrying that request id, if any.
func (l *DecisionLog) ByRequestID(rid string) (controller.Decision, bool, error) {
	row := l.DB.QueryRow(`
		SELECT id, request_id, tick_at, action, current_replicas, desired_replicas,
		       reasons_json, failure_class, failure_detail, observation_json
		FROM decisions WHERE request_id = ? ORDER BY id DESC LIMIT 1`, rid)
	d, err := scanDecision(row)
	if err == sql.ErrNoRows {
		return controller.Decision{}, false, nil
	}
	if err != nil {
		return controller.Decision{}, false, err
	}
	return d, true, nil
}

type scanner interface {
	Scan(dest ...any) error
}

func scanDecision(s scanner) (controller.Decision, error) {
	var d controller.Decision
	var action, reasonsJSON, failureClass, failureDetail, obsJSON string
	if err := s.Scan(&d.ID, &d.RequestID, &d.TickAt, &action, &d.CurrentReplicas,
		&d.DesiredReplicas, &reasonsJSON, &failureClass, &failureDetail, &obsJSON); err != nil {
		return d, err
	}
	d.Action = controller.Action(action)
	if err := json.Unmarshal([]byte(reasonsJSON), &d.Reasons); err != nil {
		return d, fmt.Errorf("unmarshal reasons: %w", err)
	}
	d.FailureClass = controller.FailureClass(failureClass)
	d.FailureDetail = failureDetail
	if obsJSON != "" {
		d.Observation = &controller.Observation{}
		if err := json.Unmarshal([]byte(obsJSON), d.Observation); err != nil {
			return d, fmt.Errorf("unmarshal observation: %w", err)
		}
	}
	return d, nil
}

// RawPointLog implements controller.RawHistory.
type RawPointLog struct {
	DB *sql.DB
}

// NewRawPointLog builds the evidence-history store.
func NewRawPointLog(db *sql.DB) *RawPointLog { return &RawPointLog{DB: db} }

// AppendPoint records one tick. Re-running the same tick overwrites rather
// than duplicating evidence (a reconcile at a given wall-clock time is one
// observation).
func (h *RawPointLog) AppendPoint(p controller.RawPoint) error {
	_, err := h.DB.Exec(
		`INSERT INTO raw_points(tick_at, raw_desired) VALUES(?,?)
		 ON CONFLICT(tick_at) DO UPDATE SET raw_desired = excluded.raw_desired`,
		p.At, p.RawDesired)
	if err != nil {
		return fmt.Errorf("insert raw point: %w", err)
	}
	return nil
}

// RawPointsSince returns evidence points in [since, now], oldest first.
func (h *RawPointLog) RawPointsSince(since, now int64) ([]controller.RawPoint, error) {
	rows, err := h.DB.Query(
		`SELECT tick_at, raw_desired FROM raw_points WHERE tick_at BETWEEN ? AND ? ORDER BY tick_at`,
		since, now)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []controller.RawPoint
	for rows.Next() {
		var p controller.RawPoint
		if err := rows.Scan(&p.At, &p.RawDesired); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}
