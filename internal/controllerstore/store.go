// Package controllerstore holds the controller's private bookkeeping in its
// own SQLite file: a decision ledger (why each reconcile step was accepted,
// rejected or undecidable), per-object attempt counters, and the highest
// actual-resource version the controller has already acted on. The last
// value is what distinguishes a genuinely older observation from a normal
// in-flight one.
package controllerstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"crcontroller/internal/model"
)

// Store is the controller's persistence layer.
type Store struct {
	db *sql.DB
}

// Open initializes the controller database.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	for _, p := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA busy_timeout=5000",
		"PRAGMA synchronous=NORMAL",
	} {
		if _, err := db.Exec(p); err != nil {
			db.Close()
			return nil, err
		}
	}
	s := &Store{db: db}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the handle.
func (s *Store) Close() error { return s.db.Close() }

const schema = `
CREATE TABLE IF NOT EXISTS object_state (
    uid                    TEXT PRIMARY KEY,
    attempts               INTEGER NOT NULL DEFAULT 0,
    last_actual_version    INTEGER NOT NULL DEFAULT 0,
    last_observed_gen      INTEGER NOT NULL DEFAULT 0,
    external_id            TEXT NOT NULL DEFAULT '',
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    uid                 TEXT NOT NULL,
    attempt             INTEGER NOT NULL,
    phase               TEXT NOT NULL,
    decision            TEXT NOT NULL,
    category            TEXT NOT NULL,
    detail              TEXT NOT NULL,
    external_id         TEXT NOT NULL DEFAULT '',
    desired_generation  INTEGER NOT NULL DEFAULT 0,
    observed_generation INTEGER NOT NULL DEFAULT 0,
    resource_version    INTEGER NOT NULL DEFAULT 0,
    actual_version      INTEGER NOT NULL DEFAULT 0,
    request_id          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_controller_ledger_uid ON ledger(uid, seq);
`

// State is the per-object bookkeeping.
type State struct {
	UID               string
	Attempts          int64
	LastActualVersion int64
	LastObservedGen   int64
	ExternalID        string
}

// GetState returns the per-object state, or a zero state if none exists.
func (s *Store) GetState(ctx context.Context, uid string) (State, error) {
	var st State
	var updated string
	err := s.db.QueryRowContext(ctx,
		`SELECT uid, attempts, last_actual_version, last_observed_gen,
		        external_id, updated_at
		 FROM object_state WHERE uid = ?`, uid).
		Scan(&st.UID, &st.Attempts, &st.LastActualVersion,
			&st.LastObservedGen, &st.ExternalID, &updated)
	if errors.Is(err, sql.ErrNoRows) {
		return State{UID: uid}, nil
	}
	if err != nil {
		return State{}, err
	}
	return st, nil
}

// IncrementAttempts bumps and returns the attempt counter for uid.
func (s *Store) IncrementAttempts(ctx context.Context, uid string) (int64, error) {
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO object_state(uid, attempts, updated_at)
		 VALUES(?, 0, ?)
		 ON CONFLICT(uid) DO NOTHING`,
		uid, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		return 0, err
	}
	if _, err := s.db.ExecContext(ctx,
		`UPDATE object_state SET attempts = attempts + 1,
		    updated_at = ? WHERE uid = ?`,
		time.Now().UTC().Format(time.RFC3339Nano), uid); err != nil {
		return 0, err
	}
	var n int64
	err := s.db.QueryRowContext(ctx,
		`SELECT attempts FROM object_state WHERE uid = ?`, uid).Scan(&n)
	return n, err
}

// RecordObservation stores the latest accepted actual version/observation so a
// future older snapshot can be identified as stale.
func (s *Store) RecordObservation(ctx context.Context, uid, externalID string,
	actualVersion, observedGen int64) error {
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO object_state(uid, attempts, last_actual_version,
		    last_observed_gen, external_id, updated_at)
		 VALUES(?, 0, ?, ?, ?, ?)
		 ON CONFLICT(uid) DO UPDATE SET
		    last_actual_version = excluded.last_actual_version,
		    last_observed_gen   = excluded.last_observed_gen,
		    external_id         = excluded.external_id,
		    updated_at         = excluded.updated_at`,
		uid, actualVersion, observedGen, externalID,
		time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		return err
	}
	return nil
}

// ClearState removes bookkeeping after a successful purge.
func (s *Store) ClearState(ctx context.Context, uid string) error {
	if _, err := s.db.ExecContext(ctx,
		`DELETE FROM object_state WHERE uid = ?`, uid); err != nil {
		return err
	}
	return nil
}

// AppendLedger stores one decision.
func (s *Store) AppendLedger(ctx context.Context, e model.LedgerEntry) (model.LedgerEntry, error) {
	ts := time.Now().UTC()
	if e.CreatedAt.IsZero() {
		e.CreatedAt = ts
	}
	res, err := s.db.ExecContext(ctx, `
INSERT INTO ledger(uid, attempt, phase, decision, category, detail,
    external_id, desired_generation, observed_generation, resource_version,
    actual_version, request_id, created_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.UID, e.Attempt, e.Phase, e.Decision, string(e.Category), e.Detail,
		e.ExternalID, e.DesiredGeneration, e.ObservedGeneration,
		e.ResourceVersion, e.ActualVersion, e.RequestID,
		e.CreatedAt.Format(time.RFC3339Nano))
	if err != nil {
		return e, err
	}
	if id, err := res.LastInsertId(); err == nil {
		e.Seq = id
	}
	return e, nil
}

// LedgerByUID returns controller ledger entries for uid.
func (s *Store) LedgerByUID(ctx context.Context, uid string, limit int) ([]model.LedgerEntry, error) {
	if limit <= 0 {
		limit = 1000
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT seq, uid, attempt, phase, decision, category, detail, external_id,
       desired_generation, observed_generation, resource_version,
       actual_version, request_id, created_at
FROM ledger WHERE uid = ? ORDER BY seq ASC LIMIT ?`, uid, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.LedgerEntry
	for rows.Next() {
		var l model.LedgerEntry
		var cat, createdAt string
		if err := rows.Scan(
			&l.Seq, &l.UID, &l.Attempt, &l.Phase, &l.Decision, &cat,
			&l.Detail, &l.ExternalID, &l.DesiredGeneration,
			&l.ObservedGeneration, &l.ResourceVersion, &l.ActualVersion,
			&l.RequestID, &createdAt); err != nil {
			return nil, err
		}
		l.Category = model.FailureCategory(cat)
		l.CreatedAt, _ = time.Parse(time.RFC3339Nano, createdAt)
		out = append(out, l)
	}
	return out, rows.Err()
}

// LatestLedger returns the most recent n controller decisions.
func (s *Store) LatestLedger(ctx context.Context, n int) ([]model.LedgerEntry, error) {
	if n <= 0 {
		n = 100
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT seq, uid, attempt, phase, decision, category, detail, external_id,
       desired_generation, observed_generation, resource_version,
       actual_version, request_id, created_at
FROM (
  SELECT seq, uid, attempt, phase, decision, category, detail, external_id,
         desired_generation, observed_generation, resource_version,
         actual_version, request_id, created_at
  FROM ledger ORDER BY seq DESC LIMIT ?
) ORDER BY seq ASC`, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.LedgerEntry
	for rows.Next() {
		var l model.LedgerEntry
		var cat, createdAt string
		if err := rows.Scan(
			&l.Seq, &l.UID, &l.Attempt, &l.Phase, &l.Decision, &cat,
			&l.Detail, &l.ExternalID, &l.DesiredGeneration,
			&l.ObservedGeneration, &l.ResourceVersion, &l.ActualVersion,
			&l.RequestID, &createdAt); err != nil {
			return nil, err
		}
		l.Category = model.FailureCategory(cat)
		l.CreatedAt, _ = time.Parse(time.RFC3339Nano, createdAt)
		out = append(out, l)
	}
	return out, rows.Err()
}
