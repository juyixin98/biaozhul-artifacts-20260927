// Package storage persists admission outcomes in SQLite and provides the
// idempotency lookup for duplicate calls.
//
// Schema is created and migrated idempotently on Open. The store is the only
// package that touches database/sql; upper layers depend on the Store
// interface so unit tests can substitute an in-memory implementation.
package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	_ "modernc.org/sqlite"

	"admission/internal/types"
)

// Store is the persistence contract.
type Store interface {
	// LookupUID returns the stored response for an idempotency key and whether
	// it exists.
	LookupUID(ctx context.Context, uid string) (types.Response, bool, error)
	// SaveResponse persists a terminal response. It is an upsert keyed by UID,
	// so a retried UID ends with exactly one stored verdict.
	SaveResponse(ctx context.Context, resp types.Response) error
	// AppendAudit writes one audit event (every attempt gets a row).
	AppendAudit(ctx context.Context, ev types.AuditEvent) error
	// RecentAudit returns the most recent audit events, newest first.
	RecentAudit(ctx context.Context, limit int) ([]types.AuditEvent, error)
	// EnqueueRetry/DequeueRetry back the reconciliation queue.
	EnqueueRetry(ctx context.Context, review types.Review, attempts int, notBeforeUnix int64) error
	DequeueRetry(ctx context.Context, nowUnix int64) (PendingItem, error)
	// RequeueRetry releases an item after a failed attempt with a new delay;
	// DoneRetry removes it.
	RequeueRetry(ctx context.Context, uid string, attempts int, notBeforeUnix int64) error
	DoneRetry(ctx context.Context, uid string) error
	CountRetry(ctx context.Context) (int, error)
	Close() error
}

// PendingItem is one dequeued retry unit.
type PendingItem struct {
	UID      string
	Attempts int
	Review   types.Review
}

// ErrNotFound is returned by DequeueRetry when nothing is due.
var ErrNotFound = errors.New("no pending item due")

// ParkedHorizon is the sentinel not_before of an in-flight item. It is large
// enough to stay out of dequeue windows but far below MaxInt64 so adding to it
// cannot overflow.
const ParkedHorizon int64 = 1 << 62

// SQLiteStore implements Store.
type SQLiteStore struct {
	db *sql.DB
}

// Open opens (creating if needed) the database file. Use ":memory:" for
// ephemeral test stores. busy_timeout and WAL make concurrent service/reconcile
// access safe in the file-backed case.
func Open(ctx context.Context, dsn string) (*SQLiteStore, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // modernc SQLite + serialized access; queue ops rely on tx ordering
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	s := &SQLiteStore{db: db}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLiteStore) migrate(ctx context.Context) error {
	stmts := []string{
		`PRAGMA journal_mode=WAL;`,
		`PRAGMA busy_timeout=5000;`,
		`CREATE TABLE IF NOT EXISTS responses (
			uid TEXT PRIMARY KEY,
			body TEXT NOT NULL,
			allowed INTEGER NOT NULL,
			finished_at INTEGER NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS audit_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			run_id TEXT NOT NULL,
			uid TEXT NOT NULL,
			attempt INTEGER NOT NULL,
			terminal INTEGER NOT NULL,
			allowed INTEGER NOT NULL,
			category TEXT NOT NULL DEFAULT '',
			body TEXT NOT NULL,
			finished_at INTEGER NOT NULL
		);`,
		`CREATE INDEX IF NOT EXISTS idx_audit_uid ON audit_events(uid);`,
		`CREATE TABLE IF NOT EXISTS retry_queue (
			uid TEXT PRIMARY KEY,
			body TEXT NOT NULL,
			attempts INTEGER NOT NULL,
			not_before INTEGER NOT NULL
		);`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("migrate: %q: %w", q, err)
		}
	}
	return nil
}

func (s *SQLiteStore) LookupUID(ctx context.Context, uid string) (types.Response, bool, error) {
	var body string
	err := s.db.QueryRowContext(ctx, `SELECT body FROM responses WHERE uid=?`, uid).Scan(&body)
	if errors.Is(err, sql.ErrNoRows) {
		return types.Response{}, false, nil
	}
	if err != nil {
		return types.Response{}, false, err
	}
	var resp types.Response
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		return types.Response{}, false, err
	}
	return resp, true, nil
}

func (s *SQLiteStore) SaveResponse(ctx context.Context, resp types.Response) error {
	body, err := json.Marshal(resp)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO responses(uid, body, allowed, finished_at) VALUES(?,?,?,?)
		ON CONFLICT(uid) DO UPDATE SET body=excluded.body, allowed=excluded.allowed,
			finished_at=excluded.finished_at`,
		resp.UID, string(body), boolInt(resp.Allowed), resp.FinishedAt.UnixNano())
	return err
}

func (s *SQLiteStore) AppendAudit(ctx context.Context, ev types.AuditEvent) error {
	body, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO audit_events(run_id, uid, attempt, terminal, allowed, category, body, finished_at)
		VALUES(?,?,?,?,?,?,?,?)`,
		ev.RunID, ev.UID, ev.Attempt, boolInt(ev.Terminal), boolInt(ev.Allowed),
		string(ev.Category), string(body), ev.FinishedAt.UnixNano())
	return err
}

func (s *SQLiteStore) RecentAudit(ctx context.Context, limit int) ([]types.AuditEvent, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT body FROM audit_events ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []types.AuditEvent
	for rows.Next() {
		var body string
		if err := rows.Scan(&body); err != nil {
			return nil, err
		}
		var ev types.AuditEvent
		if err := json.Unmarshal([]byte(body), &ev); err != nil {
			return nil, err
		}
		out = append(out, ev)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) EnqueueRetry(ctx context.Context, review types.Review, attempts int, notBeforeUnix int64) error {
	body, err := json.Marshal(review)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO retry_queue(uid, body, attempts, not_before) VALUES(?,?,?,?)
		ON CONFLICT(uid) DO UPDATE SET attempts=excluded.attempts, not_before=excluded.not_before`,
		review.UID, string(body), attempts, notBeforeUnix)
	return err
}

func (s *SQLiteStore) DequeueRetry(ctx context.Context, nowUnix int64) (PendingItem, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return PendingItem{}, err
	}
	defer func() { _ = tx.Rollback() }()

	var uid, body string
	var attempts int
	row := tx.QueryRowContext(ctx, `
		SELECT uid, body, attempts FROM retry_queue
		WHERE not_before <= ? ORDER BY not_before, uid LIMIT 1`, nowUnix)
	if err := row.Scan(&uid, &body, &attempts); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return PendingItem{}, ErrNotFound
		}
		return PendingItem{}, err
	}
	// Claim: park the item until requeue/done.
	if _, err := tx.ExecContext(ctx,
		`UPDATE retry_queue SET not_before=? WHERE uid=?`, ParkedHorizon, uid); err != nil {
		return PendingItem{}, err
	}
	if err := tx.Commit(); err != nil {
		return PendingItem{}, err
	}
	var review types.Review
	if err := json.Unmarshal([]byte(body), &review); err != nil {
		return PendingItem{}, err
	}
	return PendingItem{UID: uid, Attempts: attempts, Review: review}, nil
}

func (s *SQLiteStore) RequeueRetry(ctx context.Context, uid string, attempts int, notBeforeUnix int64) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE retry_queue SET attempts=?, not_before=? WHERE uid=?`,
		attempts, notBeforeUnix, uid)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return fmt.Errorf("requeue: uid %q not in queue", uid)
	}
	return nil
}

func (s *SQLiteStore) DoneRetry(ctx context.Context, uid string) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM retry_queue WHERE uid=?`, uid)
	return err
}

// CountRetry reports items that are due now or waiting on backoff. Rows
// parked for an in-flight attempt (initial synchronous call or a claimed
// dequeue) are not counted.
func (s *SQLiteStore) CountRetry(ctx context.Context) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM retry_queue WHERE not_before < ?`, ParkedHorizon).Scan(&n)
	return n, err
}

func (s *SQLiteStore) Close() error { return s.db.Close() }

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
