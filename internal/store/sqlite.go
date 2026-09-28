// Package store persists revisioned snapshots and reconciliation runs in
// SQLite. The implementation uses the pure-Go modernc.org/sqlite driver, so
// the build needs no CGO.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"netpolicy/internal/domain"
	"netpolicy/internal/reconcile"
)

// Store is a SQLite-backed snapshot + run log store.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the database and runs migrations.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// One connection keeps WAL writes serial; the offline server is
	// low-traffic and correctness beats throughput here.
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, `
		PRAGMA journal_mode=WAL;
		PRAGMA foreign_keys=ON;
		PRAGMA busy_timeout=5000;
	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite pragmas: %w", err)
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
	_, err := s.db.ExecContext(ctx, schema)
	return err
}

const schema = `
CREATE TABLE IF NOT EXISTS snapshots (
	revision     INTEGER PRIMARY KEY,
	source_hash  TEXT NOT NULL,
	created_at   TEXT NOT NULL,
	content      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconcile_runs (
	id            INTEGER PRIMARY KEY AUTOINCREMENT,
	started_at    TEXT NOT NULL,
	finished_at   TEXT NOT NULL,
	status        TEXT NOT NULL,
	revision      INTEGER NOT NULL,
	content_hash  TEXT NOT NULL,
	error_kind    TEXT NOT NULL DEFAULT '',
	error_message TEXT NOT NULL DEFAULT '',
	attempted     INTEGER NOT NULL DEFAULT 0
);
`

// ErrNotFound is returned when a requested revision does not exist.
var ErrNotFound = errors.New("snapshot not found")

// SaveSnapshot atomically stores a new revision. The revision is assigned as
// max(existing)+1 inside the transaction; the input snapshot's Revision
// field is set accordingly and returned. A content hash equal to the latest
// revision is not an error: callers detect "unchanged" via GetRevision.
func (s *Store) SaveSnapshot(ctx context.Context, snap *domain.Snapshot) (int64, bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, false, err
	}
	defer tx.Rollback()

	var prevRev int64
	err = tx.QueryRowContext(ctx, `SELECT revision FROM snapshots ORDER BY revision DESC LIMIT 1`).Scan(&prevRev)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		// empty history
	case err != nil:
		return 0, false, err
	}
	// Idempotency: identical content already exists at some revision
	// (including a non-latest one), so no new version is created. Label
	// snapshots and policy versions stay 1:1 with content.
	var existingRev int64
	switch err := tx.QueryRowContext(ctx, `SELECT revision FROM snapshots WHERE source_hash = ? ORDER BY revision DESC LIMIT 1`, snap.SourceHash).Scan(&existingRev); {
	case err == nil:
		if err := tx.Commit(); err != nil {
			return 0, false, err
		}
		snap.Revision = existingRev
		return existingRev, false, nil
	case errors.Is(err, sql.ErrNoRows):
		// proceed to insert
	default:
		return 0, false, err
	}
	rev := prevRev + 1
	// Stamp the revision BEFORE serializing so that a revision read back
	// from history carries the same version number it was stored under.
	snap.Revision = rev
	content, err := json.Marshal(snap)
	if err != nil {
		return 0, false, err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO snapshots(revision, source_hash, created_at, content) VALUES (?, ?, ?, ?)`,
		rev, snap.SourceHash, time.Now().UTC().Format(time.RFC3339Nano), string(content)); err != nil {
		return 0, false, err
	}
	if err := tx.Commit(); err != nil {
		return 0, false, err
	}
	return rev, true, nil
}

// GetRevision loads one revision.
func (s *Store) GetRevision(ctx context.Context, rev int64) (*domain.Snapshot, error) {
	var content string
	err := s.db.QueryRowContext(ctx, `SELECT content FROM snapshots WHERE revision = ?`, rev).Scan(&content)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("%w: revision %d", ErrNotFound, rev)
	}
	if err != nil {
		return nil, err
	}
	return decodeSnapshot(content)
}

// LatestRevision returns the revision number of the newest snapshot and
// whether any snapshot exists.
func (s *Store) LatestRevision(ctx context.Context) (int64, bool, error) {
	var rev int64
	err := s.db.QueryRowContext(ctx, `SELECT revision FROM snapshots ORDER BY revision DESC LIMIT 1`).Scan(&rev)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return rev, true, nil
}

// Latest loads the newest snapshot, or (nil,nil) when history is empty.
func (s *Store) Latest(ctx context.Context) (*domain.Snapshot, error) {
	var content string
	err := s.db.QueryRowContext(ctx, `SELECT content FROM snapshots ORDER BY revision DESC LIMIT 1`).Scan(&content)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return decodeSnapshot(content)
}

// Revisions lists stored revisions oldest-first with metadata.
func (s *Store) Revisions(ctx context.Context) ([]RevisionInfo, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT revision, source_hash, created_at FROM snapshots ORDER BY revision`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RevisionInfo
	for rows.Next() {
		var ri RevisionInfo
		if err := rows.Scan(&ri.Revision, &ri.SourceHash, &ri.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, ri)
	}
	return out, rows.Err()
}

// TrimHistory keeps at most keep newest revisions and returns how many were
// deleted. It never trims the latest revision.
func (s *Store) TrimHistory(ctx context.Context, keep int) (int64, error) {
	if keep <= 0 {
		return 0, fmt.Errorf("keep must be positive")
	}
	res, err := s.db.ExecContext(ctx, `
		DELETE FROM snapshots
		 WHERE revision NOT IN (
			SELECT revision FROM snapshots ORDER BY revision DESC LIMIT ?
		 )`, keep)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// RevisionInfo is metadata about one stored snapshot.
type RevisionInfo struct {
	Revision   int64  `json:"revision"`
	SourceHash string `json:"sourceHash"`
	CreatedAt  string `json:"createdAt"`
}

func decodeSnapshot(content string) (*domain.Snapshot, error) {
	var snap domain.Snapshot
	if err := json.Unmarshal([]byte(content), &snap); err != nil {
		return nil, fmt.Errorf("decode snapshot: %w", err)
	}
	return &snap, nil
}

// SaveRun records one reconciliation loop iteration.
func (s *Store) SaveRun(ctx context.Context, r reconcile.RunRecord) (int64, error) {
	res, err := s.db.ExecContext(ctx, `
		INSERT INTO reconcile_runs(started_at, finished_at, status, revision, content_hash, error_kind, error_message, attempted)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		r.StartedAt.UTC().Format(time.RFC3339Nano),
		r.FinishedAt.UTC().Format(time.RFC3339Nano),
		string(r.Status), r.Revision, r.ContentHash, r.ErrorKind, r.ErrorMessage, boolInt(r.Attempted))
	if err != nil {
		return 0, err
	}
	id, _ := res.LastInsertId()
	return id, nil
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// RecentRuns returns up to limit newest run records, newest first.
func (s *Store) RecentRuns(ctx context.Context, limit int) ([]reconcile.RunRecord, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, started_at, finished_at, status, revision, content_hash, error_kind, error_message, attempted
		  FROM reconcile_runs ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []reconcile.RunRecord
	for rows.Next() {
		var r reconcile.RunRecord
		var started, finished string
		var attempted int
		if err := rows.Scan(&r.ID, &started, &finished, &r.Status, &r.Revision, &r.ContentHash,
			&r.ErrorKind, &r.ErrorMessage, &attempted); err != nil {
			return nil, err
		}
		r.StartedAt, _ = time.Parse(time.RFC3339Nano, started)
		r.FinishedAt, _ = time.Parse(time.RFC3339Nano, finished)
		r.Attempted = attempted == 1
		out = append(out, r)
	}
	return out, rows.Err()
}
