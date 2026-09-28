package actualstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
)

func encodeJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "null"
	}
	return string(b)
}

func decodeJSON(s string, v any) error {
	if strings.TrimSpace(s) == "" {
		s = "null"
	}
	return json.Unmarshal([]byte(s), v)
}

func isUnique(err error) bool {
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint failed")
}

// InitSnapshotTable ensures the stale-snapshot table exists.
func (s *Store) InitSnapshotTable() error {
	_, err := s.db.Exec(`
CREATE TABLE IF NOT EXISTS resource_snapshots (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL,
    generation INTEGER NOT NULL,
    spec_hash  TEXT NOT NULL,
    spec       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    state      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_id_seq ON resource_snapshots(id, seq);
`)
	return err
}

// saveSnapshot records the current row state before it is mutated. Called in
// the same transaction as Update would be ideal, but for this local fixture a
// best-effort insert right before the UPDATE suffices: tests configure the
// fault deterministically, not concurrently.
func (s *Store) saveSnapshot(r *Resource) error {
	if r == nil {
		return nil
	}
	_, err := s.db.Exec(`
INSERT INTO resource_snapshots(id, generation, spec_hash, spec, version,
    state, created_at)
VALUES(?,?,?,?,?,?,?)`,
		r.ID, r.Generation, r.SpecHash, encodeJSON(r.Spec), r.Version, r.State,
		r.UpdatedAt.Format("2006-01-02T15:04:05.999999999Z07:00"))
	return err
}

// LatestSnapshot returns the most recent pre-update snapshot for id, if any.
// The stale-get fault serves this row so the controller observes a state
// older than what it has already advanced past.
func (s *Store) LatestSnapshot(id string) (*Resource, bool, error) {
	if err := s.InitSnapshotTable(); err != nil {
		return nil, false, err
	}
	var r row
	var created string
	err := s.db.QueryRow(`
SELECT id, '' , generation, spec_hash, spec, version, state, created_at, created_at
FROM resource_snapshots WHERE id = ? ORDER BY seq DESC LIMIT 1`, id).
		Scan(&r.ID, &r.OwnerUID, &r.Generation, &r.SpecHash, &r.Spec,
			&r.Version, &r.State, &created, &created)
	if err == sql.ErrNoRows {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	out := &Resource{
		ID: r.ID, Generation: r.Generation, SpecHash: r.SpecHash,
		Version: r.Version, State: r.State, Spec: map[string]any{},
	}
	_ = decodeJSON(r.Spec, &out.Spec)
	return out, true, nil
}

// SnapshotBeforeUpdate records the pre-update state then performs the
// conditional update within one connection (single-writer pool serializes).
func (s *Store) SnapshotBeforeUpdate(in UpdateInput) (*Resource, error) {
	cur, err := s.Get(in.ID)
	if err != nil {
		return nil, err
	}
	if cur.Version != in.ExpectedVer {
		return nil, ErrVersionConflict
	}
	if err := s.InitSnapshotTable(); err != nil {
		return nil, err
	}
	if err := s.saveSnapshot(cur); err != nil {
		return nil, err
	}
	return s.Update(in)
}

// BumpCounter atomically increments a named counter and returns the new value.
func (s *Store) BumpCounter(ctx context.Context, name string, delta int64) (int64, error) {
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO counters(name, value) VALUES(?, 0)
         ON CONFLICT(name) DO NOTHING`, name); err != nil {
		return 0, err
	}
	if _, err := s.db.ExecContext(ctx,
		`UPDATE counters SET value = value + ? WHERE name = ?`, delta, name); err != nil {
		return 0, err
	}
	var v int64
	err := s.db.QueryRowContext(ctx,
		`SELECT value FROM counters WHERE name = ?`, name).Scan(&v)
	return v, err
}

// Counters returns all counters as a map.
func (s *Store) Counters() (map[string]int64, error) {
	rows, err := s.db.Query(`SELECT name, value FROM counters`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int64{}
	for rows.Next() {
		var n string
		var v int64
		if err := rows.Scan(&n, &v); err != nil {
			return nil, err
		}
		out[n] = v
	}
	return out, rows.Err()
}

// RequestLogEntry is one recorded HTTP call against the actual service.
type RequestLogEntry struct {
	Seq          int64  `json:"seq"`
	RequestID    string `json:"requestID"`
	Method       string `json:"method"`
	Path         string `json:"path"`
	StatusCode   int    `json:"statusCode"`
	Fault        string `json:"fault,omitempty"`
	BodyRedacted string `json:"bodyRedacted,omitempty"`
	CreatedAt    string `json:"createdAt"`
}

// LogRequest records an inbound request. The body stored is already redacted.
func (s *Store) LogRequest(ctx context.Context, e RequestLogEntry) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO request_log(request_id, method, path, status_code, fault,
    body_redacted, created_at)
VALUES(?,?,?,?,?,?,?)`,
		e.RequestID, e.Method, e.Path, e.StatusCode, e.Fault, e.BodyRedacted,
		e.CreatedAt)
	return err
}

// RequestLog returns recent request log entries (newest first).
func (s *Store) RequestLog(limit int) ([]RequestLogEntry, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.Query(`
SELECT seq, request_id, method, path, status_code, fault, body_redacted,
       created_at
FROM (
  SELECT seq, request_id, method, path, status_code, fault, body_redacted,
         created_at
  FROM request_log ORDER BY seq DESC LIMIT ?
) ORDER BY seq ASC`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RequestLogEntry
	for rows.Next() {
		var e RequestLogEntry
		if err := rows.Scan(&e.Seq, &e.RequestID, &e.Method, &e.Path,
			&e.StatusCode, &e.Fault, &e.BodyRedacted, &e.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}
