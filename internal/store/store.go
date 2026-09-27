// Package store persists topology versions, bucket assignments, health
// revisions and the replay event log in SQLite (pure-Go modernc.org driver,
// no cgo required).
//
// Write ordering: every state-changing operation is persisted in one
// transaction BEFORE the in-memory ring is swapped. Replay therefore only
// needs the single monotonically ordered event_log table.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	_ "modernc.org/sqlite"

	"flexhash/internal/fherr"
)

// EventType values in the replay log.
const (
	EvConfig = "config"
	EvHealth = "health"
)

// ConfigSnapshot is one persisted topology version.
type ConfigSnapshot struct {
	Version     int64
	BucketCount int
	MembersJSON []byte // [{id,address,weight,healthy}]
}

// AssignmentRow is one persisted bucket assignment.
type AssignmentRow struct {
	Version int64
	Bucket  int
	Member  string
}

// Event is one row of the append-only replay log.
type Event struct {
	Seq  int64
	Type string
	// PayloadJSON: config -> ConfigSnapshot.MembersJSON with extra fields
	// {version,bucket_count}; health -> {member, healthy, revision}
	PayloadJSON []byte
}

// Store wraps the SQLite database handle.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the database and applies migrations.
func Open(ctx context.Context, path string) (*Store, error) {
	const op = "store.Open"
	// busy_timeout makes short lock contention a retry inside SQLite rather
	// than an immediate SQLITE_BUSY; WAL gives concurrent readers during a
	// single writer. foreign_keys and the NORMAL synchronous level are the
	// standard durable-but-fast WAL combination.
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(2000)&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=foreign_keys(ON)",
		strings.TrimPrefix(path, "file:"))
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fherr.Wrap(fherr.KindResourceExhausted, op, "cannot open sqlite handle", err)
	}
	// modernc + WAL: multiple writers still serialize; one pooled writer
	// connection avoids SQLITE_BUSY under concurrent config updates while
	// readers are served from separate connections.
	db.SetMaxOpenConns(8)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, mapErr(op, "ping", err)
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate(ctx context.Context) error {
	const op = "store.migrate"
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS meta (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS config_snapshots (
			version INTEGER PRIMARY KEY,
			bucket_count INTEGER NOT NULL,
			members_json TEXT NOT NULL,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
		`CREATE TABLE IF NOT EXISTS assignments (
			version INTEGER NOT NULL,
			bucket INTEGER NOT NULL,
			member TEXT NOT NULL,
			PRIMARY KEY (version, bucket)
		)`,
		`CREATE TABLE IF NOT EXISTS health_events (
			revision INTEGER PRIMARY KEY,
			member TEXT NOT NULL,
			healthy INTEGER NOT NULL,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
		`CREATE TABLE IF NOT EXISTS event_log (
			seq INTEGER PRIMARY KEY AUTOINCREMENT,
			kind TEXT NOT NULL,
			version INTEGER,
			revision INTEGER,
			payload_json TEXT NOT NULL,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
		`CREATE TABLE IF NOT EXISTS run_logs (
			run_id TEXT PRIMARY KEY,
			test_name TEXT NOT NULL,
			result TEXT NOT NULL,
			detail_json TEXT NOT NULL,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
	}
	for _, st := range stmts {
		if _, err := s.db.ExecContext(ctx, st); err != nil {
			return mapErr(op, "ddl", err)
		}
	}
	return nil
}

// SaveConfig persists a new topology version: snapshot, full assignment table
// and the replay event, all in one transaction.
func (s *Store) SaveConfig(ctx context.Context, snap ConfigSnapshot, assignments []AssignmentRow) error {
	const op = "store.SaveConfig"
	if len(assignments) != snap.BucketCount {
		return fherr.New(fherr.KindComputationFailed, op,
			fmt.Sprintf("assignment rows %d != bucket count %d", len(assignments), snap.BucketCount))
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return mapErr(op, "begin", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO config_snapshots(version, bucket_count, members_json) VALUES (?,?,?)`,
		snap.Version, snap.BucketCount, string(snap.MembersJSON)); err != nil {
		return mapErr(op, "insert snapshot", err)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM assignments WHERE version = ?`, snap.Version); err != nil {
		return mapErr(op, "clear assignments", err)
	}
	stmt, err := tx.PrepareContext(ctx,
		`INSERT INTO assignments(version, bucket, member) VALUES (?,?,?)`)
	if err != nil {
		return mapErr(op, "prepare assignments", err)
	}
	defer stmt.Close()
	for _, a := range assignments {
		if _, err := stmt.ExecContext(ctx, a.Version, a.Bucket, a.Member); err != nil {
			return mapErr(op, "insert assignment", err)
		}
	}
	payload, err := json.Marshal(struct {
		Version     int64           `json:"version"`
		BucketCount int             `json:"bucket_count"`
		Members     json.RawMessage `json:"members"`
	}{snap.Version, snap.BucketCount, snap.MembersJSON})
	if err != nil {
		return fherr.Wrap(fherr.KindComputationFailed, op, "encode event payload", err)
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO event_log(kind, version, revision, payload_json) VALUES (?,?,NULL,?)`,
		EvConfig, snap.Version, string(payload)); err != nil {
		return mapErr(op, "insert event", err)
	}
	if err := tx.Commit(); err != nil {
		return mapErr(op, "commit", err)
	}
	return nil
}

// SaveHealth persists one health transition and appends its replay event.
func (s *Store) SaveHealth(ctx context.Context, revision int64, member string, healthy bool) error {
	const op = "store.SaveHealth"
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return mapErr(op, "begin", err)
	}
	defer func() { _ = tx.Rollback() }()
	h := 0
	if healthy {
		h = 1
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO health_events(revision, member, healthy) VALUES (?,?,?)`,
		revision, member, h); err != nil {
		return mapErr(op, "insert health", err)
	}
	payload, err := json.Marshal(struct {
		Revision int64  `json:"revision"`
		Member   string `json:"member"`
		Healthy  bool   `json:"healthy"`
	}{revision, member, healthy})
	if err != nil {
		return fherr.Wrap(fherr.KindComputationFailed, op, "encode event payload", err)
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO event_log(kind, version, revision, payload_json) VALUES (?,NULL,?,?)`,
		EvHealth, revision, string(payload)); err != nil {
		return mapErr(op, "insert event", err)
	}
	if err := tx.Commit(); err != nil {
		return mapErr(op, "commit", err)
	}
	return nil
}

// LatestConfig returns the highest versioned snapshot, or sql.ErrNoRows.
func (s *Store) LatestConfig(ctx context.Context) (ConfigSnapshot, []AssignmentRow, error) {
	const op = "store.LatestConfig"
	var snap ConfigSnapshot
	var membersJSON string
	err := s.db.QueryRowContext(ctx,
		`SELECT version, bucket_count, members_json FROM config_snapshots
		 ORDER BY version DESC LIMIT 1`).Scan(&snap.Version, &snap.BucketCount, &membersJSON)
	if err != nil {
		return ConfigSnapshot{}, nil, mapErr(op, "select", err)
	}
	snap.MembersJSON = []byte(membersJSON)
	rows, err := s.db.QueryContext(ctx,
		`SELECT bucket, member FROM assignments WHERE version = ? ORDER BY bucket`, snap.Version)
	if err != nil {
		return ConfigSnapshot{}, nil, mapErr(op, "select assignments", err)
	}
	defer rows.Close()
	out := make([]AssignmentRow, 0, snap.BucketCount)
	for rows.Next() {
		var a AssignmentRow
		if err := rows.Scan(&a.Bucket, &a.Member); err != nil {
			return ConfigSnapshot{}, nil, mapErr(op, "scan assignment", err)
		}
		a.Version = snap.Version
		out = append(out, a)
	}
	if err := rows.Err(); err != nil {
		return ConfigSnapshot{}, nil, mapErr(op, "assignments cursor", err)
	}
	return snap, out, nil
}

// AssignmentsAt returns the persisted assignment rows for a version.
func (s *Store) AssignmentsAt(ctx context.Context, version int64) ([]AssignmentRow, error) {
	const op = "store.AssignmentsAt"
	rows, err := s.db.QueryContext(ctx,
		`SELECT bucket, member FROM assignments WHERE version = ? ORDER BY bucket`, version)
	if err != nil {
		return nil, mapErr(op, "select", err)
	}
	defer rows.Close()
	var out []AssignmentRow
	for rows.Next() {
		var a AssignmentRow
		if err := rows.Scan(&a.Bucket, &a.Member); err != nil {
			return nil, mapErr(op, "scan", err)
		}
		a.Version = version
		out = append(out, a)
	}
	if len(out) == 0 {
		return nil, fherr.New(fherr.KindStateConflict, op,
			fmt.Sprintf("no assignments persisted for version %d", version))
	}
	return out, mapErr(op, "cursor", rows.Err())
}

// LatestHealthRevision returns the highest health revision (0 if none).
func (s *Store) LatestHealthRevision(ctx context.Context) (int64, error) {
	const op = "store.LatestHealthRevision"
	var rev sql.NullInt64
	if err := s.db.QueryRowContext(ctx,
		`SELECT MAX(revision) FROM health_events`).Scan(&rev); err != nil {
		return 0, mapErr(op, "select", err)
	}
	if !rev.Valid {
		return 0, nil
	}
	return rev.Int64, nil
}

// Events returns the full ordered replay log.
func (s *Store) Events(ctx context.Context) ([]Event, error) {
	const op = "store.Events"
	rows, err := s.db.QueryContext(ctx,
		`SELECT seq, kind, payload_json FROM event_log ORDER BY seq`)
	if err != nil {
		return nil, mapErr(op, "select", err)
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var e Event
		if err := rows.Scan(&e.Seq, &e.Type, &e.PayloadJSON); err != nil {
			return nil, mapErr(op, "scan", err)
		}
		out = append(out, e)
	}
	return out, mapErr(op, "cursor", rows.Err())
}

// SaveRunLog records a test/run outcome for later problem replay.
func (s *Store) SaveRunLog(ctx context.Context, runID, testName, result string, detail any) error {
	const op = "store.SaveRunLog"
	b, err := json.Marshal(detail)
	if err != nil {
		return fherr.Wrap(fherr.KindComputationFailed, op, "encode run detail", err)
	}
	if _, err := s.db.ExecContext(ctx,
		`INSERT OR REPLACE INTO run_logs(run_id, test_name, result, detail_json) VALUES (?,?,?,?)`,
		runID, testName, result, string(b)); err != nil {
		return mapErr(op, "insert", err)
	}
	return nil
}

// RunLog is a persisted run record.
type RunLog struct {
	RunID      string
	TestName   string
	Result     string
	DetailJSON []byte
	CreatedAt  string
}

// RunLogs returns run records newest first, up to limit.
func (s *Store) RunLogs(ctx context.Context, limit int) ([]RunLog, error) {
	const op = "store.RunLogs"
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT run_id, test_name, result, detail_json, created_at
		 FROM run_logs ORDER BY created_at DESC, rowid DESC LIMIT ?`, limit)
	if err != nil {
		return nil, mapErr(op, "select", err)
	}
	defer rows.Close()
	var out []RunLog
	for rows.Next() {
		var r RunLog
		if err := rows.Scan(&r.RunID, &r.TestName, &r.Result, &r.DetailJSON, &r.CreatedAt); err != nil {
			return nil, mapErr(op, "scan", err)
		}
		out = append(out, r)
	}
	return out, mapErr(op, "cursor", rows.Err())
}

// DB exposes the handle for tests that need to corrupt/close it.
func (s *Store) DB() *sql.DB { return s.db }

// mapErr translates SQLite errors into the typed taxonomy.
func mapErr(op, step string, err error) error {
	if err == nil {
		return nil
	}
	msg := fmt.Sprintf("%s: %v", step, err)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return fherr.Wrap(fherr.KindStateConflict, op, "no rows: "+msg, err)
	case strings.Contains(err.Error(), "BUSY"), strings.Contains(err.Error(), "locked"):
		return fherr.Wrap(fherr.KindStateConflict, op, "database locked: "+msg, err)
	case strings.Contains(err.Error(), "UNIQUE"):
		return fherr.Wrap(fherr.KindStateConflict, op, "unique constraint: "+msg, err)
	case strings.Contains(err.Error(), "FOREIGN KEY"):
		return fherr.Wrap(fherr.KindStateConflict, op, "foreign key: "+msg, err)
	case strings.Contains(err.Error(), "too many"),
		strings.Contains(err.Error(), "FULL"),
		strings.Contains(err.Error(), "nomem"),
		strings.Contains(err.Error(), "out of memory"),
		strings.Contains(err.Error(), "connection"):
		return fherr.Wrap(fherr.KindResourceExhausted, op, "resource limit: "+msg, err)
	default:
		return fherr.Wrap(fherr.KindComputationFailed, op, msg, err)
	}
}
