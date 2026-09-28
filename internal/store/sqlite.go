// Package store persists computation requests, results and verification
// evidence in SQLite, and provides the replay/audit queries. It deliberately
// defines a small interface so the HTTP layer never sees database/sql types.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite" // pure-Go driver, registered as "sqlite".
)

// Record is one persisted computation.
type Record struct {
	RequestID  string
	Family     string
	Width      int
	AllowJSON  string
	ExclJSON   string
	Status     string // "ok" | "error"
	ResultJSON string
	ErrorCode  string
	ErrorText  string
	Warnings   string
	StepsJSON  string
	ClientRef  string
	CreatedAt  time.Time
}

// SummaryRow is one aggregated replay entry.
type SummaryRow struct {
	RequestID string   `json:"request_id"`
	Status    string   `json:"status"`
	Family    string   `json:"family"`
	Width     int      `json:"width"`
	Prefixes  []string `json:"prefixes"`
	ErrorCode string   `json:"error_code,omitempty"`
	ErrorText string   `json:"error_text,omitempty"`
	ClientRef string   `json:"client_ref,omitempty"`
	CreatedAt string   `json:"created_at"`
}

// Store is the persistence surface used by the service.
type Store interface {
	Save(ctx context.Context, r Record) error
	Get(ctx context.Context, requestID string) (*Record, error)
	List(ctx context.Context, limit, offset int, family, status string) ([]Record, error)
	CountByStatus(ctx context.Context) (map[string]int, error)
	Close() error
}

// ErrNotFound is returned by Get for unknown ids.
var ErrNotFound = errors.New("record not found")

// SQLiteStore implements Store over a single SQLite database.
type SQLiteStore struct {
	db *sql.DB
}

// Open opens (creating the schema in) the database at dsn. Use
// "file::memory:?cache=shared" for a shared in-memory database.
func Open(ctx context.Context, dsn string) (*SQLiteStore, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// modernc.org/sqlite is safe with a single connection for our write
	// pattern; a busy_timeout avoids "database is locked" under concurrency.
	if _, err := db.ExecContext(ctx, "PRAGMA busy_timeout = 5000"); err != nil {
		db.Close()
		return nil, fmt.Errorf("pragma: %w", err)
	}
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode = WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("pragma wal: %w", err)
	}
	s := &SQLiteStore{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLiteStore) migrate(ctx context.Context) error {
	const schema = `
CREATE TABLE IF NOT EXISTS computations (
    request_id   TEXT PRIMARY KEY,
    family       TEXT NOT NULL,
    width        INTEGER NOT NULL,
    allow_json   TEXT NOT NULL,
    exclude_json TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ok','error')),
    result_json  TEXT NOT NULL DEFAULT '[]',
    error_code   TEXT NOT NULL DEFAULT '',
    error_text   TEXT NOT NULL DEFAULT '',
    warnings     TEXT NOT NULL DEFAULT '[]',
    steps_json   TEXT NOT NULL DEFAULT '[]',
    client_ref   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_computations_created ON computations(created_at);
CREATE INDEX IF NOT EXISTS idx_computations_status_family ON computations(status, family);
`
	if _, err := s.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	return nil
}

// Save inserts one record. A duplicate request id is reported.
func (s *SQLiteStore) Save(ctx context.Context, r Record) error {
	if r.CreatedAt.IsZero() {
		r.CreatedAt = time.Now().UTC()
	}
	_, err := s.db.ExecContext(ctx, `
INSERT INTO computations
(request_id, family, width, allow_json, exclude_json, status, result_json,
 error_code, error_text, warnings, steps_json, client_ref, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		r.RequestID, r.Family, r.Width, r.AllowJSON, r.ExclJSON, r.Status,
		r.ResultJSON, r.ErrorCode, r.ErrorText, r.Warnings, r.StepsJSON,
		r.ClientRef, r.CreatedAt.Format(time.RFC3339Nano))
	if err != nil {
		return fmt.Errorf("save record %s: %w", r.RequestID, err)
	}
	return nil
}

// Get fetches a single record.
func (s *SQLiteStore) Get(ctx context.Context, id string) (*Record, error) {
	row := s.db.QueryRowContext(ctx, `
SELECT request_id, family, width, allow_json, exclude_json, status, result_json,
       error_code, error_text, warnings, steps_json, client_ref, created_at
FROM computations WHERE request_id = ?`, id)
	r, err := scanRecord(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("%w: %s", ErrNotFound, id)
	}
	if err != nil {
		return nil, err
	}
	return r, nil
}

// List returns records newest first with optional family/status filters.
func (s *SQLiteStore) List(ctx context.Context, limit, offset int, family, status string) ([]Record, error) {
	q := `
SELECT request_id, family, width, allow_json, exclude_json, status, result_json,
       error_code, error_text, warnings, steps_json, client_ref, created_at
FROM computations`
	var args []any
	var where []string
	if family != "" {
		where = append(where, "family = ?")
		args = append(args, family)
	}
	if status != "" {
		where = append(where, "status = ?")
		args = append(args, status)
	}
	if len(where) > 0 {
		q += " WHERE " + joinStrings(where, " AND ")
	}
	q += " ORDER BY created_at DESC, request_id DESC LIMIT ? OFFSET ?"
	args = append(args, limit, offset)

	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("list: %w", err)
	}
	defer rows.Close()
	var out []Record
	for rows.Next() {
		r, err := scanRecord(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *r)
	}
	return out, rows.Err()
}

// CountByStatus returns aggregate counts for the replay dashboard.
func (s *SQLiteStore) CountByStatus(ctx context.Context) (map[string]int, error) {
	rows, err := s.db.QueryContext(ctx,
		"SELECT status, COUNT(*) FROM computations GROUP BY status")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{"ok": 0, "error": 0}
	for rows.Next() {
		var k string
		var n int
		if err := rows.Scan(&k, &n); err != nil {
			return nil, err
		}
		out[k] = n
	}
	return out, rows.Err()
}

func (s *SQLiteStore) Close() error { return s.db.Close() }

type rowScanner interface {
	Scan(dest ...any) error
}

func scanRecord(sc rowScanner) (*Record, error) {
	var r Record
	var created string
	if err := sc.Scan(
		&r.RequestID, &r.Family, &r.Width, &r.AllowJSON, &r.ExclJSON, &r.Status,
		&r.ResultJSON, &r.ErrorCode, &r.ErrorText, &r.Warnings, &r.StepsJSON,
		&r.ClientRef, &created,
	); err != nil {
		return nil, err
	}
	r.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	return &r, nil
}

func joinStrings(ss []string, sep string) string {
	out := ""
	for i, s := range ss {
		if i > 0 {
			out += sep
		}
		out += s
	}
	return out
}
