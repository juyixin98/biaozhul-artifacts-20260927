// Package store persists the coordinator's state in SQLite. Every state
// transition that must be atomic — approving an eviction while reserving its
// budget slot, invalidating an approval while recording its reclamation,
// ingesting an observation together with the involuntary failures it reveals
// — runs inside a single BEGIN IMMEDIATE transaction.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"evictor/internal/domain"
)

// Store wraps an SQLite database. One database file, opened with a busy
// timeout, serializes writers; BEGIN IMMEDIATE takes the write lock up front
// so concurrent approvals get SQLITE_BUSY-and-retry rather than a deadlock.
type Store struct {
	db *sql.DB
}

// DBTX is satisfied by both *sql.DB and *sql.Tx so every query runs the same
// inside or outside a transaction.
type DBTX interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// Open opens (creating if needed) the database and applies migrations.
func Open(ctx context.Context, dsn string) (*Store, error) {
	if !strings.Contains(dsn, "_txlock") {
		sep := "?"
		if strings.Contains(dsn, "?") {
			sep = "&"
		}
		dsn += sep + "_txlock=immediate"
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // SQLite: avoid lock-contention churn; all txns are serial
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.db.Close() }

// DB exposes the handle for tests that need raw access; application code uses
// the typed methods below.
func (s *Store) DB() *sql.DB { return s.db }

// BeginImmediate starts a write transaction. The DSN sets _txlock=immediate,
// so modernc issues `BEGIN IMMEDIATE` for this transaction, taking SQLite's
// reserved lock up front: a losing concurrent approver fails at begin time
// and WithTx retries it instead of deadlocking at commit.
func (s *Store) BeginImmediate(ctx context.Context) (*sql.Tx, error) {
	return s.db.BeginTx(ctx, nil)
}

// WithTx runs fn inside a single immediate transaction, retrying
// SQLITE_BUSY/LOCKED a bounded number of times. This is the ONLY place retry
// logic lives: admission paths call WithTx and rely on it for the
// concurrent-approver interleaving.
func (s *Store) WithTx(ctx context.Context, fn func(q DBTX) error) error {
	const maxAttempts = 20
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		tx, err := s.BeginImmediate(ctx)
		if err != nil {
			if isBusy(err) {
				lastErr = err
				time.Sleep(time.Duration(attempt+1) * 5 * time.Millisecond)
				continue
			}
			return err
		}
		err = fn(tx)
		if err == nil {
			if err := tx.Commit(); err != nil {
				if isBusy(err) {
					lastErr = err
					continue
				}
				return err
			}
			return nil
		}
		_ = tx.Rollback()
		if errors.Is(err, ErrRetry) || isBusy(err) {
			lastErr = err
			time.Sleep(time.Duration(attempt+1) * 5 * time.Millisecond)
			continue
		}
		return err
	}
	return fmt.Errorf("store: transaction still locked after %d attempts: %w", maxAttempts, lastErr)
}

// ErrRetry marks a transaction that should be retried from scratch (the
// coordinator returns it when its snapshot became stale mid-transaction).
var ErrRetry = errors.New("store: retry transaction")

func isBusy(err error) bool {
	if err == nil {
		return false
	}
	m := err.Error()
	return strings.Contains(m, "SQLITE_BUSY") || strings.Contains(m, "SQLITE_LOCKED") ||
		strings.Contains(m, "database is locked")
}

// ---------------------------------------------------------------------------
// Row types
// ---------------------------------------------------------------------------

type PolicyRow struct {
	Group             string
	MinAvailable      int
	MaxUnavailable    int
	ApproveTTL        time.Duration
	CompletionTimeout time.Duration
}

type SelectorRow struct {
	Group        string
	Version      int64
	MatchLabels  string
	EffectiveAt  time.Time
	SupersededAt time.Time // zero when still active
}

func (s SelectorRow) Active() bool { return s.SupersededAt.IsZero() }

type InstanceRow struct {
	ID             string
	Group          string
	Labels         string // canonical k=v,k=v
	SelVersion     int64
	LastObservedAt time.Time // zero if never observed
	State          string    // state from the latest observation, "" if never observed
}

type EvictionRow struct {
	ID              string
	Group           string
	InstanceID      string
	Phase           string
	OutcomeReason   string
	Detail          string
	SelectorVersion int64
	CreatedAt       time.Time
	ApprovedAt      time.Time
	ExpiresAt       time.Time
	TerminalAt      time.Time
	ObservationID   int64 // 0 if null
}

func (e EvictionRow) Open() bool { return e.Phase == string(domain.PhaseApproved) }

type ReclaimRow struct {
	ID          int64
	EvictionID  string
	Kind        string // completion | ttl-expiry | involuntary-failure | selector-change
	Detail      string
	CreatedAt   time.Time
	ConfirmedAt time.Time
	ConfirmedBy string
}

type ObservationRow struct {
	ID         int64
	Group      string
	ObservedAt time.Time
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

func (s *Store) migrate(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, schemaSQL)
	return err
}

const schemaSQL = `
CREATE TABLE IF NOT EXISTS groups (
	group_name              TEXT PRIMARY KEY,
	min_available           INTEGER NOT NULL,
	max_unavailable         INTEGER NOT NULL,
	approve_ttl_ms          INTEGER NOT NULL,
	completion_timeout_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS selectors (
	group_name    TEXT NOT NULL,
	version       INTEGER NOT NULL,
	match_labels  TEXT NOT NULL,
	effective_at  INTEGER NOT NULL,
	superseded_at INTEGER,
	PRIMARY KEY (group_name, version)
);

CREATE TABLE IF NOT EXISTS instances (
	instance_id      TEXT PRIMARY KEY,
	group_name       TEXT NOT NULL,
	labels           TEXT NOT NULL,
	selector_version INTEGER NOT NULL DEFAULT 1,
	last_observed_at INTEGER
);

CREATE TABLE IF NOT EXISTS observations (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	group_name  TEXT NOT NULL,
	observed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_rows (
	observation_id INTEGER NOT NULL REFERENCES observations(id),
	instance_id    TEXT NOT NULL,
	state          TEXT NOT NULL,
	PRIMARY KEY (observation_id, instance_id)
);

CREATE TABLE IF NOT EXISTS evictions (
	id               TEXT PRIMARY KEY,
	group_name       TEXT NOT NULL,
	instance_id      TEXT NOT NULL,
	phase            TEXT NOT NULL,            -- approved|completed|failed|expired|revoked
	outcome_reason   TEXT NOT NULL DEFAULT '',
	detail           TEXT NOT NULL DEFAULT '',
	selector_version INTEGER NOT NULL,
	created_at       INTEGER NOT NULL,
	approved_at      INTEGER,
	expires_at       INTEGER,
	terminal_at      INTEGER,
	observation_id   INTEGER
);
-- At most one OPEN eviction per instance. Database-enforced, so even a buggy
-- caller (or a racing pair of requests) can never double-reserve a slot.
CREATE UNIQUE INDEX IF NOT EXISTS ux_evictions_open_instance
	ON evictions(instance_id) WHERE phase = 'approved';

CREATE INDEX IF NOT EXISTS ix_evictions_group_phase
	ON evictions(group_name, phase);

-- Every invalidated approval leaves an explicit, separately retrievable
-- confirmation record. Reservations are never freed silently.
CREATE TABLE IF NOT EXISTS reclaim_events (
	id           INTEGER PRIMARY KEY AUTOINCREMENT,
	eviction_id  TEXT NOT NULL REFERENCES evictions(id),
	kind         TEXT NOT NULL,
	detail       TEXT NOT NULL DEFAULT '',
	created_at   INTEGER NOT NULL,
	confirmed_at INTEGER NOT NULL,
	confirmed_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostics (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	ts          INTEGER NOT NULL,
	request_id  TEXT NOT NULL,
	group_name  TEXT NOT NULL,
	instance_id TEXT NOT NULL DEFAULT '',
	action      TEXT NOT NULL,
	outcome     TEXT NOT NULL,
	reason      TEXT NOT NULL DEFAULT '',
	detail      TEXT NOT NULL DEFAULT '',
	budget_json TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_diagnostics_group_ts ON diagnostics(group_name, ts);
`

// ---------------------------------------------------------------------------
// Policies / selectors / instances
// ---------------------------------------------------------------------------

func (s *Store) UpsertPolicy(ctx context.Context, p PolicyRow) error {
	return s.WithTx(ctx, func(q DBTX) error { return upsertPolicy(q, ctx, p) })
}

func upsertPolicy(q DBTX, ctx context.Context, p PolicyRow) error {
	_, err := q.ExecContext(ctx, `
INSERT INTO groups(group_name, min_available, max_unavailable, approve_ttl_ms, completion_timeout_ms)
VALUES(?, ?, ?, ?, ?)
ON CONFLICT(group_name) DO UPDATE SET
	min_available=excluded.min_available,
	max_unavailable=excluded.max_unavailable,
	approve_ttl_ms=excluded.approve_ttl_ms,
	completion_timeout_ms=excluded.completion_timeout_ms`,
		p.Group, p.MinAvailable, p.MaxUnavailable,
		p.ApproveTTL.Milliseconds(), p.CompletionTimeout.Milliseconds())
	return err
}

func getPolicy(q DBTX, ctx context.Context, group string) (PolicyRow, error) {
	row := q.QueryRowContext(ctx, `
SELECT group_name, min_available, max_unavailable, approve_ttl_ms, completion_timeout_ms
FROM groups WHERE group_name = ?`, group)
	var p PolicyRow
	var ttl, drainTO int64
	if err := row.Scan(&p.Group, &p.MinAvailable, &p.MaxUnavailable, &ttl, &drainTO); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return PolicyRow{}, fmt.Errorf("%w: policy for group %q", ErrNotFound, group)
		}
		return PolicyRow{}, err
	}
	p.ApproveTTL = time.Duration(ttl) * time.Millisecond
	p.CompletionTimeout = time.Duration(drainTO) * time.Millisecond
	return p, nil
}

// PublishSelector publishes a selector version for a group. If the current
// active selector already has exactly these labels the SAME version is
// returned (a no-op republish is not a version change). Otherwise a new
// version is inserted and the previous one marked superseded — all in one tx.
func (s *Store) PublishSelector(ctx context.Context, group, labels string, now time.Time) (SelectorRow, bool, error) {
	var out SelectorRow
	var created bool
	err := s.WithTx(ctx, func(q DBTX) error {
		cur, err := currentSelector(q, ctx, group)
		if err == nil && cur.MatchLabels == labels {
			out = cur
			return nil
		}
		if err != nil && !errors.Is(err, ErrNotFound) {
			return err
		}
		var next int64 = 1
		if err == nil {
			next = cur.Version + 1
			if _, err := q.ExecContext(ctx,
				`UPDATE selectors SET superseded_at = ? WHERE group_name = ? AND version = ?`,
				now.UnixMilli(), group, cur.Version); err != nil {
				return err
			}
		}
		if _, err := q.ExecContext(ctx, `
INSERT INTO selectors(group_name, version, match_labels, effective_at, superseded_at)
VALUES(?, ?, ?, ?, NULL)`, group, next, labels, now.UnixMilli()); err != nil {
			return err
		}
		out = SelectorRow{Group: group, Version: next, MatchLabels: labels, EffectiveAt: now}
		created = true
		return nil
	})
	return out, created, err
}

func currentSelector(q DBTX, ctx context.Context, group string) (SelectorRow, error) {
	row := q.QueryRowContext(ctx, `
SELECT group_name, version, match_labels, effective_at, COALESCE(superseded_at, 0)
FROM selectors WHERE group_name = ? AND superseded_at IS NULL`, group)
	return scanSelector(row)
}

// CurrentSelector returns the active selector for a group.
func (s *Store) CurrentSelector(ctx context.Context, group string) (SelectorRow, error) {
	var sel SelectorRow
	err := s.WithTx(ctx, func(q DBTX) error {
		var err error
		sel, err = currentSelector(q, ctx, group)
		return err
	})
	return sel, err
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanSelector(r rowScanner) (SelectorRow, error) {
	var sel SelectorRow
	var eff, sup int64
	if err := r.Scan(&sel.Group, &sel.Version, &sel.MatchLabels, &eff, &sup); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return SelectorRow{}, fmt.Errorf("%w: selector", ErrNotFound)
		}
		return SelectorRow{}, err
	}
	sel.EffectiveAt = time.UnixMilli(eff)
	if sup > 0 {
		sel.SupersededAt = time.UnixMilli(sup)
	}
	return sel, nil
}

// EnsureInstance registers topology membership. Existing rows keep their id.
func (s *Store) EnsureInstance(ctx context.Context, id, group, labels string, selVersion int64) error {
	return s.WithTx(ctx, func(q DBTX) error {
		_, err := q.ExecContext(ctx, `
INSERT INTO instances(instance_id, group_name, labels, selector_version)
VALUES(?, ?, ?, ?)
ON CONFLICT(instance_id) DO UPDATE SET
	group_name=excluded.group_name,
	labels=excluded.labels,
	selector_version=excluded.selector_version`, id, group, labels, selVersion)
		return err
	})
}

// ErrNotFound is the package-level not-found sentinel.
var ErrNotFound = errors.New("store: not found")
