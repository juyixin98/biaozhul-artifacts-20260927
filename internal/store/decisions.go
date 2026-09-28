package store

import (
	"context"
	"encoding/json"
	"time"

	"replicactl/internal/model"
)

// SaveDecision appends a decision to the audit log.
func (s *Store) SaveDecision(ctx context.Context, d model.Decision) error {
	payload, err := json.Marshal(d)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO decisions(request_id,tick_at,action,payload) VALUES(?,?,?,?)`,
		d.RequestID, d.TickAt.UTC().Format(time.RFC3339Nano), d.Action, string(payload))
	return err
}

// Decisions returns recent decisions, newest first, limited to limit rows.
func (s *Store) Decisions(ctx context.Context, limit int) ([]model.Decision, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT payload FROM decisions ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Decision
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, err
		}
		var d model.Decision
		if err := json.Unmarshal([]byte(p), &d); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// DecisionByRequestID fetches a single decision record by its request id.
func (s *Store) DecisionByRequestID(ctx context.Context, requestID string) (model.Decision, error) {
	var p string
	err := s.db.QueryRowContext(ctx,
		`SELECT payload FROM decisions WHERE request_id=? ORDER BY id DESC LIMIT 1`, requestID).Scan(&p)
	if err != nil {
		return model.Decision{}, err
	}
	var d model.Decision
	if err := json.Unmarshal([]byte(p), &d); err != nil {
		return model.Decision{}, err
	}
	return d, nil
}
