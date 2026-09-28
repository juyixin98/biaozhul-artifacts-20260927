// Package storage owns all SQLite persistence: the request ledger (with
// processing state for re-entrant reconciliation), committed resources,
// audit records with per-step replays, and a transactional per-kind quota
// ledger that implements plugins.QuotaService.
//
// The package exposes one concrete type, *Store, built on database/sql with
// the mattn/go-sqlite3 driver. No other package writes SQL.
package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"admission/internal/model"

	_ "github.com/mattn/go-sqlite3"
)

// Request statuses stored in the requests table.
const (
	StatusPending    = "pending"
	StatusProcessing = "processing"
	StatusAllowed    = "allowed"
	StatusDenied     = "denied"
	StatusFailed     = "failed" // infrastructure failure before a verdict was reached
)

// ErrConflict is returned when a UID is already present in a state that
// cannot accept another submission attempt.
var ErrConflict = errors.New("request uid conflict")

// ErrNotFound is returned for missing rows.
var ErrNotFound = errors.New("not found")

// Store is the persistence boundary.
type Store struct {
	db          *sql.DB
	quotaLimits map[string]int64
}

// SetQuotaLimits installs the per-kind capacity map consulted by the
// authoritative commit-time capacity gate. Kinds absent from the map are
// unlimited.
func (s *Store) SetQuotaLimits(limits map[string]int64) { s.quotaLimits = limits }

// Open opens (creating the schema in) the database at dsn. ":memory:" is
// supported for tests. The DSN may carry sqlite query parameters
// ("file.db?_busy_timeout=5000"); the parent directory of the file part is
// created when needed.
func Open(ctx context.Context, dsn string) (*Store, error) {
	path := dsn
	if i := strings.IndexByte(dsn, '?'); i >= 0 {
		path = dsn[:i]
	}
	if path != "" && path != ":memory:" && !strings.HasPrefix(path, "file:") {
		if dir := filepath.Dir(path); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, fmt.Errorf("create database directory: %w", err)
			}
		}
	}
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, err
	}
	// Single connection serializes writers and makes the per-transaction
	// quota check safe without extra locking.
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS requests (
			uid          TEXT PRIMARY KEY,
			operation    TEXT NOT NULL,
			payload      TEXT NOT NULL,
			status       TEXT NOT NULL,
			reason       TEXT NOT NULL DEFAULT '',
			message      TEXT NOT NULL DEFAULT '',
			final_digest TEXT NOT NULL DEFAULT '',
			attempts     INTEGER NOT NULL DEFAULT 0,
			lease_until  INTEGER NOT NULL DEFAULT 0,
			updated_at   INTEGER NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)`,
		`CREATE TABLE IF NOT EXISTS resources (
			kind      TEXT NOT NULL,
			namespace TEXT NOT NULL DEFAULT '',
			name      TEXT NOT NULL,
			uid       TEXT NOT NULL,
			doc       TEXT NOT NULL,
			replicas  INTEGER NOT NULL DEFAULT 0,
			updated_at INTEGER NOT NULL,
			PRIMARY KEY (kind, namespace, name)
		)`,
		`CREATE TABLE IF NOT EXISTS audits (
			id     INTEGER PRIMARY KEY AUTOINCREMENT,
			uid    TEXT NOT NULL,
			run_id TEXT NOT NULL,
			status TEXT NOT NULL,
			reason TEXT NOT NULL DEFAULT '',
			summary_digest TEXT NOT NULL DEFAULT '',
			record TEXT NOT NULL,
			created_at INTEGER NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_audits_uid ON audits(uid)`,
		`CREATE TABLE IF NOT EXISTS quota_ledger (
			uid  TEXT NOT NULL,
			kind TEXT NOT NULL,
			delta INTEGER NOT NULL,
			committed INTEGER NOT NULL DEFAULT 0,
			PRIMARY KEY (uid, kind)
		)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("migrate: %w", err)
		}
	}
	return nil
}

// DB exposes the raw handle for the SQLQuota and tests.
func (s *Store) DB() *sql.DB { return s.db }

// InsertRequest records a new pending request. A duplicate UID whose terminal
// verdict matches the same payload is returned to the coordinator for
// idempotent replay lookup; distinct payloads on an active UID are a conflict.
// The returned bool is true when the row already existed (caller should load
// the stored outcome instead of reprocessing).
func (s *Store) InsertRequest(ctx context.Context, payload string, uid string, op model.Operation, nowMs int64) (existed bool, err error) {
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO requests (uid, operation, payload, status, updated_at)
		 VALUES (?, ?, ?, ?, ?)
		 ON CONFLICT(uid) DO NOTHING`,
		uid, string(op), payload, StatusPending, nowMs)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 0, nil
}

// RequestRow is one requests-table projection.
type RequestRow struct {
	UID         string
	Operation   model.Operation
	Payload     string
	Status      string
	Reason      string
	Message     string
	FinalDigest string
	Attempts    int
	LeaseUntil  int64
}

// GetRequest loads a request by UID.
func (s *Store) GetRequest(ctx context.Context, uid string) (RequestRow, error) {
	var r RequestRow
	err := s.db.QueryRowContext(ctx,
		`SELECT uid, operation, payload, status, reason, message, final_digest, attempts, lease_until
		 FROM requests WHERE uid = ?`, uid).
		Scan(&r.UID, &r.Operation, &r.Payload, &r.Status, &r.Reason, &r.Message, &r.FinalDigest, &r.Attempts, &r.LeaseUntil)
	if errors.Is(err, sql.ErrNoRows) {
		return RequestRow{}, fmt.Errorf("%w: request %s", ErrNotFound, uid)
	}
	return r, err
}

// ClaimUID puts a specific request into processing under a fresh lease. Used by
// the synchronous admission path immediately after insert; stale leases are
// reclaimable because the predicate also accepts expired processing rows.
func (s *Store) ClaimUID(ctx context.Context, uid string, nowMs, leaseUntilMs int64) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE requests SET status = ?, attempts = attempts + 1, lease_until = ?, updated_at = ?
		 WHERE uid = ? AND (status IN (?, ?) OR (status = ? AND lease_until < ?))`,
		StatusProcessing, leaseUntilMs, nowMs, uid,
		StatusPending, StatusFailed, StatusProcessing, nowMs)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return fmt.Errorf("%w: request %s is not claimable", ErrConflict, uid)
	}
	return nil
}

// Claim marks up to one pending (or expired processing) request as processing
// atomically and returns its payload. The leaseUntilMs makes a crashed worker's
// claim reclaimable by the reconciler.
func (s *Store) Claim(ctx context.Context, nowMs, leaseUntilMs int64) (uid, payload string, ok bool, err error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", "", false, err
	}
	defer tx.Rollback()
	err = tx.QueryRowContext(ctx,
		`SELECT uid, payload FROM requests
		 WHERE status = ? OR (status = ? AND lease_until < ?)
		 ORDER BY updated_at ASC LIMIT 1`,
		StatusPending, StatusProcessing, nowMs).Scan(&uid, &payload)
	if errors.Is(err, sql.ErrNoRows) {
		return "", "", false, nil
	}
	if err != nil {
		return "", "", false, err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE requests SET status = ?, attempts = attempts + 1, lease_until = ?, updated_at = ?
		 WHERE uid = ?`,
		StatusProcessing, leaseUntilMs, nowMs, uid); err != nil {
		return "", "", false, err
	}
	if err := tx.Commit(); err != nil {
		return "", "", false, err
	}
	return uid, payload, true, nil
}

// ListPendingUIDs returns UIDs in pending/expired-processing state (used by
// the reconciler tests to observe progression).
func (s *Store) ListPendingUIDs(ctx context.Context, nowMs int64) ([]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT uid FROM requests
		 WHERE status = ? OR (status = ? AND lease_until < ?)
		 ORDER BY updated_at ASC`, StatusPending, StatusProcessing, nowMs)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		out = append(out, uid)
	}
	return out, rows.Err()
}
