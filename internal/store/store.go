// Package store persists cover requests and their results in SQLite so that
// results can be retrieved and replayed deterministically. It uses the
// pure-Go modernc.org/sqlite driver (no cgo), so storage works on any
// platform with a plain `go build`.
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

// Store wraps an open SQLite database.
type Store struct {
	db *sql.DB
}

// Record is one persisted request/result pair.
type Record struct {
	RequestID    string
	CreatedAt    time.Time
	AllowInput   string // JSON array as submitted
	ExcludeInput string // JSON array as submitted
	Status       string
	ResultJSON   string // full engine.Result JSON
}

// ErrNotFound is returned by Get for unknown request ids.
var ErrNotFound = errors.New("request not found")

// ErrDuplicate is returned by Save when the request id already exists.
var ErrDuplicate = errors.New("request id already exists")

// Open opens (creating the schema if needed) the database at dsn. Use
// ":memory:" for an ephemeral database. A busy timeout makes concurrent test
// writers predictable instead of failing with SQLITE_BUSY.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", dsn, err)
	}
	// SQLite is file-local; one writer connection avoids lock contention.
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, `PRAGMA journal_mode=WAL;
		PRAGMA busy_timeout=5000;
		PRAGMA foreign_keys=ON;`); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite pragmas: %w", err)
	}
	s := &Store{db: db}
	if err := s.init(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) init(ctx context.Context) error {
	const schema = `
CREATE TABLE IF NOT EXISTS requests (
    request_id     TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    allow_input    TEXT NOT NULL,
    exclude_input  TEXT NOT NULL,
    status         TEXT NOT NULL,
    result_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);
`
	if _, err := s.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("create schema: %w", err)
	}
	return nil
}

// Save inserts one record. A duplicate request_id returns ErrDuplicate.
func (s *Store) Save(ctx context.Context, r Record) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO requests
		(request_id, created_at, allow_input, exclude_input, status, result_json)
		VALUES (?, ?, ?, ?, ?, ?)`,
		r.RequestID, r.CreatedAt.UTC().Format(time.RFC3339Nano),
		r.AllowInput, r.ExcludeInput, r.Status, r.ResultJSON)
	if err != nil {
		if strings.Contains(err.Error(), "UNIQUE constraint failed") {
			return fmt.Errorf("%w: %s", ErrDuplicate, r.RequestID)
		}
		return fmt.Errorf("save request %s: %w", r.RequestID, err)
	}
	return nil
}

// Get retrieves a record by id.
func (s *Store) Get(ctx context.Context, requestID string) (Record, error) {
	row := s.db.QueryRowContext(ctx, `SELECT request_id, created_at, allow_input,
		exclude_input, status, result_json FROM requests WHERE request_id = ?`, requestID)
	var r Record
	var created string
	if err := row.Scan(&r.RequestID, &created, &r.AllowInput, &r.ExcludeInput,
		&r.Status, &r.ResultJSON); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Record{}, fmt.Errorf("%w: %s", ErrNotFound, requestID)
		}
		return Record{}, err
	}
	if t, err := time.Parse(time.RFC3339Nano, created); err == nil {
		r.CreatedAt = t
	}
	return r, nil
}

// Recent returns up to limit most recent records (newest first).
func (s *Store) Recent(ctx context.Context, limit int) ([]Record, error) {
	if limit <= 0 {
		limit = 20
	}
	rows, err := s.db.QueryContext(ctx, `SELECT request_id, created_at, allow_input,
		exclude_input, status, result_json FROM requests
		ORDER BY created_at DESC, request_id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Record
	for rows.Next() {
		var r Record
		var created string
		if err := rows.Scan(&r.RequestID, &created, &r.AllowInput, &r.ExcludeInput,
			&r.Status, &r.ResultJSON); err != nil {
			return nil, err
		}
		if t, err := time.Parse(time.RFC3339Nano, created); err == nil {
			r.CreatedAt = t
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }
