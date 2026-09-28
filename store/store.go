// Package store persists replay runs and their artifacts in SQLite.
//
// Persistence boundaries:
//
//	runs        — one row per run: status, outcome codes, the exact
//	              scenario bytes (replayability), the result and any error;
//	deliveries  — synthetic external events as delivered, with run version;
//	decisions   — every best-path change with deciding reason;
//	traces      — full ordered trace including loop rejection / policy
//	              denial / propagation decisions.
//
// The store depends on no package above it; replay maps engine artifacts
// onto its record types.
package store

import (
	"database/sql"
	"errors"
	"fmt"

	_ "modernc.org/sqlite"
)

// Store is an SQLite-backed run archive.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the database at path and applies the
// schema. Use ":memory:" for an ephemeral database.
func Open(path string) (*Store, error) {
	dsn := "file:" + path + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", path, err)
	}
	// Single connection keeps the in-memory database alive and serializes
	// writes deterministically; replay throughput is not a goal here.
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping sqlite %q: %w", path, err)
	}
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate() error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS runs (
			run_id              TEXT PRIMARY KEY,
			name                TEXT NOT NULL,
			status              TEXT NOT NULL,
			converged           INTEGER NOT NULL DEFAULT 0,
			non_convergent_code TEXT NOT NULL DEFAULT '',
			steps               INTEGER NOT NULL DEFAULT 0,
			versions            INTEGER NOT NULL DEFAULT 0,
			cycle_json          TEXT NOT NULL DEFAULT '',
			scenario_json       TEXT NOT NULL DEFAULT '',
			result_json         TEXT NOT NULL DEFAULT '',
			error_kind          TEXT NOT NULL DEFAULT '',
			error_code          TEXT NOT NULL DEFAULT '',
			error_message       TEXT NOT NULL DEFAULT '',
			created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
		`CREATE TABLE IF NOT EXISTS deliveries (
			run_id  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
			version INTEGER NOT NULL,
			seq     INTEGER NOT NULL,
			router  TEXT NOT NULL,
			peer    TEXT NOT NULL,
			kind    TEXT NOT NULL,
			prefix  TEXT NOT NULL,
			PRIMARY KEY (run_id, version)
		)`,
		`CREATE TABLE IF NOT EXISTS decisions (
			id        INTEGER PRIMARY KEY AUTOINCREMENT,
			run_id    TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
			version   INTEGER NOT NULL,
			router    TEXT NOT NULL,
			prefix    TEXT NOT NULL,
			prev_peer TEXT NOT NULL DEFAULT '',
			chosen_peer TEXT NOT NULL DEFAULT '',
			runner_up TEXT NOT NULL DEFAULT '',
			reason    TEXT NOT NULL,
			attrs_json TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE TABLE IF NOT EXISTS traces (
			id       INTEGER PRIMARY KEY AUTOINCREMENT,
			run_id   TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
			version  INTEGER NOT NULL,
			category TEXT NOT NULL,
			router   TEXT NOT NULL,
			peer     TEXT NOT NULL DEFAULT '',
			prefix   TEXT NOT NULL DEFAULT '',
			detail   TEXT NOT NULL DEFAULT '',
			attrs_before_json TEXT NOT NULL DEFAULT '',
			attrs_after_json  TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id, id)`,
		`CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id, id)`,
	}
	for _, q := range stmts {
		if _, err := s.db.Exec(q); err != nil {
			return fmt.Errorf("migrate: %w", err)
		}
	}
	return nil
}

// Run statuses.
const (
	StatusOK           = "ok"
	StatusNotConverged = "not_converged"
	StatusError        = "error"
)

// ErrNotFound is returned for unknown run ids.
var ErrNotFound = errors.New("run not found")

// ErrExists is returned when a run id is already taken.
var ErrExists = errors.New("run already exists")
