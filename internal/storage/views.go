package storage

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"tcpreplay/internal/reassembly"
)

func insertView(tx *sql.Tx, requestID string, v reassembly.GenerationView) error {
	raw, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("marshal view %s#%d: %w", v.Flow, v.Generation, err)
	}
	if _, err := tx.Exec(
		`INSERT INTO generation_views(request_id, flow, generation, view_json)
		 VALUES(?,?,?,?)
		 ON CONFLICT(request_id, flow, generation) DO UPDATE SET view_json=excluded.view_json`,
		requestID, v.Flow, v.Generation, string(raw)); err != nil {
		return fmt.Errorf("insert view %s#%d: %w", v.Flow, v.Generation, err)
	}
	return nil
}

// ListViews returns every stored generation view for a request.
func (s *Store) ListViews(requestID string) ([]reassembly.GenerationView, error) {
	rows, err := s.db.Query(
		`SELECT view_json FROM generation_views WHERE request_id=?
		 ORDER BY flow ASC, generation ASC`, requestID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []reassembly.GenerationView
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var v reassembly.GenerationView
		if err := json.Unmarshal([]byte(raw), &v); err != nil {
			return nil, fmt.Errorf("corrupt view row: %w", err)
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// GetView returns one generation view.
func (s *Store) GetView(requestID, flow string, generation int) (reassembly.GenerationView, error) {
	var raw string
	err := s.db.QueryRow(
		`SELECT view_json FROM generation_views WHERE request_id=? AND flow=? AND generation=?`,
		requestID, flow, generation).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return reassembly.GenerationView{}, ErrNotFound
	}
	if err != nil {
		return reassembly.GenerationView{}, err
	}
	var v reassembly.GenerationView
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		return reassembly.GenerationView{}, fmt.Errorf("corrupt view row: %w", err)
	}
	return v, nil
}
