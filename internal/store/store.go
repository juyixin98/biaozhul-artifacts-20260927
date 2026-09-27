// Package store persists replay runs in SQLite: the submitted config, the
// final report (including full decision trace and cycle evidence), and the
// failure category when a run aborts. Storage errors are translated to the
// ierr contract (computation_failed / not_found / resource_exhausted).
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"pathvector/internal/ierr"
)

// Store is a SQLite-backed run archive.
type Store struct {
	db *sql.DB
}

// Open opens (creating the schema if needed) a SQLite database at dsn.
// A ":memory:" DSN yields an in-process private database.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, ierr.Wrap(ierr.KindComputationFailed, "store.Open", "open "+dsn, err)
	}
	// modernc.org/sqlite is single-writer; serialize access and avoid
	// cross-connection pool surprises.
	db.SetMaxOpenConns(1)
	s := &Store{db: db}
	if err := s.init(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) init(ctx context.Context) error {
	stmts := []string{
		`PRAGMA journal_mode=WAL;`,
		`PRAGMA synchronous=NORMAL;`,
		`CREATE TABLE IF NOT EXISTS runs (
			run_id        TEXT PRIMARY KEY,
			created_at    TEXT NOT NULL,
			status        TEXT NOT NULL,
			reason        TEXT NOT NULL,
			steps         INTEGER NOT NULL,
			budget        INTEGER NOT NULL,
			error_kind    TEXT NOT NULL DEFAULT '',
			error_detail  TEXT NOT NULL DEFAULT '',
			config_json   TEXT NOT NULL,
			report_json   TEXT NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS run_events (
			run_id     TEXT NOT NULL,
			step       INTEGER NOT NULL,
			payload    TEXT NOT NULL,
			PRIMARY KEY (run_id, step)
		);`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return ierr.Wrap(ierr.KindComputationFailed, "store.init", "schema DDL failed", err)
		}
	}
	return nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// SaveInput is one persisted run.
type SaveInput struct {
	RunID       string
	Status      string
	Reason      string
	Steps       int
	Budget      int
	ErrorKind   string // "" for clean runs
	ErrorDetail string
	ConfigJSON  []byte
	Report      any
}

// SaveRun upserts the run record and replaces its trace events.
func (s *Store) SaveRun(ctx context.Context, in SaveInput) error {
	const op = "store.SaveRun"
	reportJSON, err := json.Marshal(in.Report)
	if err != nil {
		return ierr.Wrap(ierr.KindComputationFailed, op, "encode report", err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return ierr.Wrap(ierr.KindComputationFailed, op, "begin tx", err)
	}
	defer func() { _ = tx.Rollback() }()

	_, err = tx.ExecContext(ctx, `
		INSERT INTO runs(run_id, created_at, status, reason, steps, budget,
			error_kind, error_detail, config_json, report_json)
		VALUES(?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(run_id) DO UPDATE SET
			status=excluded.status, reason=excluded.reason, steps=excluded.steps,
			budget=excluded.budget, error_kind=excluded.error_kind,
			error_detail=excluded.error_detail, config_json=excluded.config_json,
			report_json=excluded.report_json`,
		in.RunID, time.Now().UTC().Format(time.RFC3339Nano),
		in.Status, in.Reason, in.Steps, in.Budget,
		in.ErrorKind, in.ErrorDetail, string(in.ConfigJSON), string(reportJSON))
	if err != nil {
		return mapSQLErr(op, err)
	}

	// Trace events are stored individually for indexed forensic queries
	// (replay-a-problem run lookup by step).
	var report struct {
		Trace []json.RawMessage `json:"trace"`
	}
	if err := json.Unmarshal(reportJSON, &report); err == nil {
		if _, err := tx.ExecContext(ctx, `DELETE FROM run_events WHERE run_id=?`, in.RunID); err != nil {
			return mapSQLErr(op, err)
		}
		for i, ev := range report.Trace {
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO run_events(run_id, step, payload) VALUES(?,?,?)`,
				in.RunID, i+1, string(ev)); err != nil {
				return mapSQLErr(op, err)
			}
		}
	}
	if err := tx.Commit(); err != nil {
		return mapSQLErr(op, err)
	}
	return nil
}

// Summary is the list-view row.
type Summary struct {
	RunID     string `json:"run_id"`
	CreatedAt string `json:"created_at"`
	Status    string `json:"status"`
	Reason    string `json:"reason"`
	Steps     int    `json:"steps"`
	Budget    int    `json:"budget"`
	ErrorKind string `json:"error_kind"`
}

// ListRuns returns newest runs first, at most limit.
func (s *Store) ListRuns(ctx context.Context, limit int) ([]Summary, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT run_id, created_at, status, reason, steps, budget, error_kind
		FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, mapSQLErr("store.ListRuns", err)
	}
	defer rows.Close()
	var out []Summary
	for rows.Next() {
		var v Summary
		if err := rows.Scan(&v.RunID, &v.CreatedAt, &v.Status, &v.Reason,
			&v.Steps, &v.Budget, &v.ErrorKind); err != nil {
			return nil, mapSQLErr("store.ListRuns", err)
		}
		out = append(out, v)
	}
	return out, mapSQLErr("store.ListRuns", rows.Err())
}

// RunRecord is the full stored run.
type RunRecord struct {
	Summary
	ConfigJSON  string `json:"-"`
	ReportJSON  string `json:"-"`
	ErrorDetail string `json:"error_detail"`
}

// GetRun fetches one run by id.
func (s *Store) GetRun(ctx context.Context, runID string) (*RunRecord, error) {
	var v RunRecord
	err := s.db.QueryRowContext(ctx, `
		SELECT run_id, created_at, status, reason, steps, budget,
			error_kind, error_detail, config_json, report_json
		FROM runs WHERE run_id=?`, runID).Scan(
		&v.RunID, &v.CreatedAt, &v.Status, &v.Reason, &v.Steps, &v.Budget,
		&v.ErrorKind, &v.ErrorDetail, &v.ConfigJSON, &v.ReportJSON)
	if err == sql.ErrNoRows {
		return nil, ierr.New(ierr.KindNotFound, "store.GetRun", "no run "+runID)
	}
	if err != nil {
		return nil, mapSQLErr("store.GetRun", err)
	}
	return &v, nil
}

// GetTrace returns the stored trace events as raw JSON rows, oldest first.
func (s *Store) GetTrace(ctx context.Context, runID string) ([]json.RawMessage, error) {
	var exists string
	err := s.db.QueryRowContext(ctx, `SELECT run_id FROM runs WHERE run_id=?`, runID).Scan(&exists)
	if err == sql.ErrNoRows {
		return nil, ierr.New(ierr.KindNotFound, "store.GetTrace", "no run "+runID)
	}
	if err != nil {
		return nil, mapSQLErr("store.GetTrace", err)
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT payload FROM run_events WHERE run_id=? ORDER BY step`, runID)
	if err != nil {
		return nil, mapSQLErr("store.GetTrace", err)
	}
	defer rows.Close()
	var out []json.RawMessage
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, mapSQLErr("store.GetTrace", err)
		}
		out = append(out, json.RawMessage(p))
	}
	return out, mapSQLErr("store.GetTrace", rows.Err())
}

func mapSQLErr(op string, err error) error {
	if err == nil {
		return nil
	}
	msg := err.Error()
	// SQLite full / too-big surfaces map to resource exhaustion.
	if containsAny(msg, "SQLITE_FULL", "SQLITE_TOOBIG", "too many", "database or disk is full") {
		return ierr.Wrap(ierr.KindResourceExhausted, op, fmt.Sprintf("sqlite limit: %s", msg), err)
	}
	return ierr.Wrap(ierr.KindComputationFailed, op, msg, err)
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
