// Package store persists replay runs in SQLite: the full result as JSON
// for exact reload, plus normalized event/transition/interval rows so the
// history can be inspected with the sqlite3 CLI or any SQL client.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"igmpq/internal/cats"
	"igmpq/internal/replay"
)

const schema = `
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    idx      INTEGER NOT NULL,
    time_ms  INTEGER NOT NULL,
    type     TEXT NOT NULL,
    iface    TEXT NOT NULL,
    grp      TEXT NOT NULL DEFAULT '',
    member   TEXT NOT NULL DEFAULT '',
    gen      INTEGER,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS transitions (
    run_id  INTEGER NOT NULL REFERENCES runs(id),
    seq     INTEGER NOT NULL,
    time_ms INTEGER NOT NULL,
    type    TEXT NOT NULL,
    iface   TEXT NOT NULL DEFAULT '',
    grp     TEXT NOT NULL DEFAULT '',
    member  TEXT NOT NULL DEFAULT '',
    gen     INTEGER,
    reason  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS intervals (
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    iface    TEXT NOT NULL,
    grp      TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER
);
CREATE TABLE IF NOT EXISTS rejections (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    event_index INTEGER NOT NULL,
    category    TEXT NOT NULL,
    reason      TEXT NOT NULL
);
`

// Store wraps the SQLite handle.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the SQLite database at dsn, e.g.
// "file:igmpq.db" or ":memory:".
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// A single connection keeps ":memory:" databases and transactions sane.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	return &Store{db: db}, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

// SaveRun persists a scenario and its result, returning the run id.
func (s *Store) SaveRun(ctx context.Context, sc replay.Scenario, res *replay.Result) (int64, error) {
	cfgJSON, err := json.Marshal(res.Config)
	if err != nil {
		return 0, err
	}
	resJSON, err := json.Marshal(res)
	if err != nil {
		return 0, err
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	r, err := tx.ExecContext(ctx,
		`INSERT INTO runs (name, config_json, result_json, created_at) VALUES (?,?,?,?)`,
		sc.Name, string(cfgJSON), string(resJSON), time.Now().UTC().Format(time.RFC3339))
	if err != nil {
		return 0, err
	}
	runID, err := r.LastInsertId()
	if err != nil {
		return 0, err
	}

	for i, ev := range sc.Events {
		var gen any
		if ev.Gen != nil {
			gen = int64(*ev.Gen)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO events (run_id, idx, time_ms, type, iface, grp, member, gen) VALUES (?,?,?,?,?,?,?,?)`,
			runID, i, ev.TimeMS, ev.Type, ev.Iface, ev.Group, ev.Member, gen); err != nil {
			return 0, err
		}
	}
	for _, t := range res.Transitions {
		var gen any
		if t.Gen != nil {
			gen = int64(*t.Gen)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO transitions (run_id, seq, time_ms, type, iface, grp, member, gen, reason, detail) VALUES (?,?,?,?,?,?,?,?,?,?)`,
			runID, t.Seq, t.TimeMS, t.Type, t.Iface, t.Group, t.Member, gen, t.Reason, t.Detail); err != nil {
			return 0, err
		}
	}
	for key, ivs := range res.Intervals {
		iface, grp := splitKey(key)
		for _, iv := range ivs {
			var end any
			if iv.EndMS != nil {
				end = *iv.EndMS
			}
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO intervals (run_id, iface, grp, start_ms, end_ms) VALUES (?,?,?,?,?)`,
				runID, iface, grp, iv.StartMS, end); err != nil {
				return 0, err
			}
		}
	}
	for _, rj := range res.Rejections {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO rejections (run_id, event_index, category, reason) VALUES (?,?,?,?)`,
			runID, rj.EventIndex, rj.Category, rj.Reason); err != nil {
			return 0, err
		}
	}

	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return runID, nil
}

// LoadResult reloads the stored result of a run.
func (s *Store) LoadResult(ctx context.Context, id int64) (*replay.Result, error) {
	var raw string
	err := s.db.QueryRowContext(ctx, `SELECT result_json FROM runs WHERE id = ?`, id).Scan(&raw)
	if err == sql.ErrNoRows {
		return nil, cats.New(cats.RunNotFound, fmt.Sprintf("no replay run with id %d", id))
	}
	if err != nil {
		return nil, err
	}
	var res replay.Result
	if err := json.Unmarshal([]byte(raw), &res); err != nil {
		return nil, fmt.Errorf("decode stored result for run %d: %w", id, err)
	}
	res.RunID = id
	return &res, nil
}

// RunMeta is one row of the run listing.
type RunMeta struct {
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	CreatedAt string `json:"created_at"`
}

// ListRuns returns stored runs, newest first.
func (s *Store) ListRuns(ctx context.Context) ([]RunMeta, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, name, created_at FROM runs ORDER BY id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RunMeta
	for rows.Next() {
		var m RunMeta
		if err := rows.Scan(&m.ID, &m.Name, &m.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

func splitKey(key string) (iface, grp string) {
	for i := 0; i < len(key); i++ {
		if key[i] == '/' {
			return key[:i], key[i+1:]
		}
	}
	return key, ""
}
