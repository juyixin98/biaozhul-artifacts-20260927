// Package store is the SQLite-backed persistence layer. It owns the schema and
// the transaction discipline; the coordinator expresses intent and this layer
// guarantees that "reserve budget + persist approval" is one atomic write.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// ErrNotFound is the sentinel storage callers map onto *not_found categories.
var ErrNotFound = errors.New("store: not found")

// Store wraps the database handle.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the SQLite database at dsn and applies the
// pragmas required for correct concurrent behaviour:
//   - WAL + busy_timeout: concurrent approvers retry on lock instead of failing;
//   - foreign_keys: membership/approvals integrity;
//   - busy_timeout is also set per connection below.
func Open(ctx context.Context, dsn string) (*Store, error) {
	if dsn == "" {
		dsn = ":memory:"
	}
	// modernc.org/sqlite registers as "sqlite".
	pragmas := "_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)&_pragma=synchronous(NORMAL)"
	full := dsn
	if dsn != ":memory:" {
		full = fmt.Sprintf("%s?%s", dsn, pragmas)
	}
	db, err := sql.Open("sqlite", full)
	if err != nil {
		return nil, err
	}
	// A single connection serialises writers and makes BEGIN IMMEDIATE behave
	// deterministically; WAL still permits concurrent readers. Combined with
	// busy_timeout this is the documented robust setting for modernc.org/sqlite.
	db.SetMaxOpenConns(1)
	if dsn == ":memory:" {
		if _, err := db.ExecContext(ctx,
			`PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;`); err != nil {
			return nil, err
		}
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.db.Close() }

// DB exposes the low-level handle for the read-only diagnostics endpoint.
func (s *Store) DB() *sql.DB { return s.db }

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS groups_meta (
			namespace TEXT NOT NULL,
			name TEXT NOT NULL,
			replicas INTEGER NOT NULL,
			budget_mode TEXT NOT NULL,
			budget_value INTEGER NOT NULL,
			budget_percent INTEGER NOT NULL,
			selector_labels TEXT NOT NULL,
			selector_epoch INTEGER NOT NULL,
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			PRIMARY KEY (namespace, name)
		)`,
		// instances are discovered via observations, but membership metadata is
		// stored so selector matching can be re-evaluated on epoch changes.
		`CREATE TABLE IF NOT EXISTS instances (
			id TEXT PRIMARY KEY,
			namespace TEXT NOT NULL,
			group_name TEXT NOT NULL,
			labels TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS observations (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			instance_id TEXT NOT NULL,
			ready INTEGER NOT NULL,
			epoch INTEGER NOT NULL,
			source TEXT NOT NULL,
			at TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_obs_instance ON observations(instance_id, id DESC)`,
		`CREATE TABLE IF NOT EXISTS failures (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			instance_id TEXT NOT NULL,
			reason TEXT NOT NULL,
			epoch INTEGER NOT NULL,
			source TEXT NOT NULL,
			at TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_fail_instance ON failures(instance_id, id DESC)`,
		`CREATE TABLE IF NOT EXISTS approvals (
			id TEXT PRIMARY KEY,
			namespace TEXT NOT NULL,
			group_name TEXT NOT NULL,
			instance_id TEXT NOT NULL,
			epoch INTEGER NOT NULL,
			state TEXT NOT NULL,
			reserved_at TEXT NOT NULL,
			expires_at TEXT NOT NULL,
			reclaimed_at TEXT,
			result_reason TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE INDEX IF NOT EXISTS idx_app_group ON approvals(namespace, group_name, state)`,
		`CREATE INDEX IF NOT EXISTS idx_app_instance ON approvals(instance_id, state)`,
		`CREATE TABLE IF NOT EXISTS decisions_log (
			request_id TEXT PRIMARY KEY,
			namespace TEXT NOT NULL,
			group_name TEXT NOT NULL,
			instance_id TEXT NOT NULL,
			accepted INTEGER NOT NULL,
			category TEXT NOT NULL,
			reason TEXT NOT NULL,
			approval_id TEXT NOT NULL DEFAULT '',
			snapshot TEXT NOT NULL,
			at TEXT NOT NULL
		)`,
	}
	for _, st := range stmts {
		if _, err := s.db.ExecContext(ctx, st); err != nil {
			return fmt.Errorf("migrate: %w\n-- %s", err, st)
		}
	}
	// Additive column upgrade for databases created before approval_id existed.
	// Ignored when the column is already present.
	if _, err := s.db.ExecContext(ctx,
		`ALTER TABLE decisions_log ADD COLUMN approval_id TEXT NOT NULL DEFAULT ''`); err != nil {
		if !strings.Contains(err.Error(), "duplicate column") {
			return fmt.Errorf("migrate approval_id: %w", err)
		}
	}
	return nil
}

func nowTS(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }
