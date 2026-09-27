package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"natlab/internal/model"
)

// SQLite persists runs, mappings, tombstones and the decision log in one local
// file. It is the durable backend for "replay the problem later": reruns open
// the same DB and read back every decision with its intermediate state.
type SQLite struct {
	db *sql.DB
}

// OpenSQLite opens path (":memory:" or a local file), applying WAL and a busy
// timeout suitable for single-process replay.
func OpenSQLite(path string) (*SQLite, error) {
	if path == "" {
		path = ":memory:"
	}
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)")
	if err != nil {
		return nil, fmt.Errorf("storage: open sqlite %q: %w", path, err)
	}
	// Single writer is plenty for a replay tool and removes lock surprises.
	db.SetMaxOpenConns(1)
	s := &SQLite{db: db}
	if err := s.migrate(context.Background()); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLite) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS runs(
			id TEXT PRIMARY KEY,
			name TEXT NOT NULL DEFAULT '',
			config_json TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL,
			watermark TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE TABLE IF NOT EXISTS mappings(
			run_id TEXT NOT NULL,
			id TEXT NOT NULL,
			proto TEXT NOT NULL,
			int_src_ip TEXT NOT NULL,
			int_src_port INTEGER NOT NULL,
			rem_ip TEXT NOT NULL,
			rem_port INTEGER NOT NULL,
			external_ip TEXT NOT NULL,
			external_port INTEGER NOT NULL,
			state TEXT NOT NULL,
			created_at TEXT NOT NULL,
			last_seen TEXT NOT NULL,
			expires_at TEXT NOT NULL,
			PRIMARY KEY(run_id, id)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_mappings_port ON mappings(run_id, proto, external_port)`,
		`CREATE TABLE IF NOT EXISTS tombstones(
			run_id TEXT NOT NULL,
			proto TEXT NOT NULL,
			external_port INTEGER NOT NULL,
			remote_ip TEXT NOT NULL,
			remote_port INTEGER NOT NULL,
			closed_at TEXT NOT NULL,
			retain_until TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_tombs ON tombstones(run_id, proto, external_port, remote_ip, remote_port)`,
		`CREATE TABLE IF NOT EXISTS decisions(
			run_id TEXT NOT NULL,
			seq INTEGER NOT NULL,
			payload TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_decisions ON decisions(run_id, seq)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("storage: migrate: %w", err)
		}
	}
	return nil
}

func ts(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }
func pts(v string) time.Time {
	t, _ := time.Parse(time.RFC3339Nano, v)
	return t
}

func (s *SQLite) UpsertRun(ctx context.Context, r RunInfo) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO runs(id,name,config_json,created_at,watermark)
		 VALUES(?,?,?,?,COALESCE((SELECT watermark FROM runs WHERE id=?), ''))
		 ON CONFLICT(id) DO UPDATE SET name=excluded.name, config_json=excluded.config_json`,
		r.ID, r.Name, r.ConfigJSON, ts(r.CreatedAt), r.ID)
	return err
}

func (s *SQLite) GetRun(ctx context.Context, id string) (RunInfo, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id,name,config_json,created_at,watermark FROM runs WHERE id=?`, id)
	var r RunInfo
	var created, wm string
	if err := row.Scan(&r.ID, &r.Name, &r.ConfigJSON, &created, &wm); err != nil {
		if err == sql.ErrNoRows {
			return RunInfo{}, &ErrNotFound{What: "run " + id}
		}
		return RunInfo{}, err
	}
	r.CreatedAt = pts(created)
	r.Watermark = pts(wm)
	return r, nil
}

func (s *SQLite) PutMapping(ctx context.Context, m StoredMapping) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO mappings(run_id,id,proto,int_src_ip,int_src_port,rem_ip,rem_port,
			external_ip,external_port,state,created_at,last_seen,expires_at)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(run_id,id) DO UPDATE SET
		   state=excluded.state, last_seen=excluded.last_seen, expires_at=excluded.expires_at`,
		m.RunID, m.ID, string(m.Proto), m.IntSrcIP, m.IntSrcPort,
		m.ExtDstIP, m.ExtDstPort, m.ExternalIP, m.ExternalPort, m.State,
		ts(m.CreatedAt), ts(m.LastSeen), ts(m.ExpiresAt))
	return err
}

func (s *SQLite) UpdateMapping(ctx context.Context, m StoredMapping) error {
	return s.PutMapping(ctx, m)
}

func (s *SQLite) DeleteMapping(ctx context.Context, runID, id string, _ time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`DELETE FROM mappings WHERE run_id=? AND id=?`, runID, id)
	return err
}

func (s *SQLite) ListMappings(ctx context.Context, runID string) ([]StoredMapping, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id,proto,int_src_ip,int_src_port,rem_ip,rem_port,external_ip,external_port,
		        state,created_at,last_seen,expires_at
		 FROM mappings WHERE run_id=? ORDER BY external_port, id`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StoredMapping
	for rows.Next() {
		var m StoredMapping
		var proto, created, seen, expires string
		if err := rows.Scan(&m.ID, &proto, &m.IntSrcIP, &m.IntSrcPort,
			&m.ExtDstIP, &m.ExtDstPort, &m.ExternalIP, &m.ExternalPort,
			&m.State, &created, &seen, &expires); err != nil {
			return nil, err
		}
		m.RunID = runID
		m.Proto = model.Protocol(proto)
		m.CreatedAt, m.LastSeen, m.ExpiresAt = pts(created), pts(seen), pts(expires)
		out = append(out, m)
	}
	return out, rows.Err()
}

func (s *SQLite) AddTombstone(ctx context.Context, t StoredTombstone) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO tombstones(run_id,proto,external_port,remote_ip,remote_port,
			closed_at,retain_until) VALUES(?,?,?,?,?,?,?)`,
		t.RunID, string(t.Proto), t.ExternalPort, t.RemoteIP, t.RemotePort,
		ts(t.ClosedAt), ts(t.RetainUntil))
	return err
}

func (s *SQLite) FindTombstone(ctx context.Context, runID string, proto model.Protocol,
	extPort uint16, remoteIP string, remotePort uint16, at time.Time) (bool, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT 1 FROM tombstones
		 WHERE run_id=? AND proto=? AND external_port=? AND remote_ip=? AND remote_port=?
		   AND retain_until > ? LIMIT 1`,
		runID, string(proto), extPort, remoteIP, remotePort, ts(at))
	var one int
	switch err := row.Scan(&one); err {
	case nil:
		return true, nil
	case sql.ErrNoRows:
		return false, nil
	default:
		return false, err
	}
}

func (s *SQLite) PruneTombstones(ctx context.Context, runID string, at time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`DELETE FROM tombstones WHERE run_id=? AND retain_until <= ?`, runID, ts(at))
	return err
}

func (s *SQLite) AppendDecision(ctx context.Context, d model.Decision) error {
	payload, err := json.Marshal(d)
	if err != nil {
		return fmt.Errorf("storage: marshal decision: %w", err)
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO decisions(run_id,seq,payload) VALUES(?,?,?)`,
		d.RunID, d.Seq, string(payload))
	return err
}

func (s *SQLite) ListDecisions(ctx context.Context, runID string, fromSeq int64,
	limit int) ([]model.Decision, error) {
	q := `SELECT payload FROM decisions WHERE run_id=? AND seq>=? ORDER BY seq`
	args := []any{runID, fromSeq}
	if limit > 0 {
		q += ` LIMIT ?`
		args = append(args, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Decision
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		var d model.Decision
		if err := json.Unmarshal([]byte(payload), &d); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

func (s *SQLite) SetWatermark(ctx context.Context, runID string, at time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE runs SET watermark=? WHERE id=? AND (watermark='' OR watermark<?)`,
		ts(at), runID, ts(at))
	return err
}

// Close releases the database handle.
func (s *SQLite) Close() error { return s.db.Close() }
