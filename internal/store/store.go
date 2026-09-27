// Package store is the durable state layer backed by SQLite.
//
// It persists three kinds of history that together make behavior replayable:
//
//   - config_revisions : every loaded/reloaded configuration blob and why;
//   - ring_versions    : every published routing generation with its member
//     set, health states and integer vnode allocation;
//   - decisions        : individual routing decisions tied to a flow set /
//     replay run (this is what replay re-evaluates).
//
// Failure mapping:
//
//   - SQLITE_BUSY / SQLITE_LOCKED are translated to
//     RESOURCE_EXHAUSTED/STORE_BUSY (a bounded local resource is unavailable;
//     the request may succeed on retry). The busy_timeout pragma bounds the
//     wait before this is returned.
//   - all other database failures become RESOURCE_EXHAUSTED/STORE_IO for
//     write-path operational failures. Input validation errors are raised by
//     callers before touching the store.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"

	"flowrouter/internal/apperr"
)

// Store wraps an SQLite database handle.
type Store struct {
	db *sql.DB
}

// Open opens (and migrates) the database at path. Use ":memory:" for tests.
// A single connection is used so SQLite serializes writes for us; readers
// (which under WAL would be concurrent) still scale enough for this service
// and the test/CI footprint stays small.
func Open(ctx context.Context, path string, busyTimeoutMs int) (*Store, error) {
	if busyTimeoutMs < 0 {
		return nil, apperr.Invalid("BAD_BUSY_TIMEOUT", "busy_timeout_ms must be >= 0")
	}
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout%%28%d%%29&_pragma=journal_mode%%28WAL%%29&_pragma=foreign_keys%%28on%%29",
		path, busyTimeoutMs)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, apperr.Exhausted("STORE_IO", "cannot open sqlite").WithCause(err)
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, mapErr(err)
	}
	s := &Store{db: db}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS config_revisions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			loaded_at TEXT NOT NULL,
			source TEXT NOT NULL,
			sha256 TEXT NOT NULL,
			body TEXT NOT NULL,
			note TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE TABLE IF NOT EXISTS ring_versions (
			version INTEGER PRIMARY KEY,
			created_at TEXT NOT NULL,
			change TEXT NOT NULL,
			members_json TEXT NOT NULL,
			allocation_json TEXT NOT NULL,
			fingerprint TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS replay_runs (
			run_id TEXT PRIMARY KEY,
			created_at TEXT NOT NULL,
			flow_set_name TEXT NOT NULL,
			flow_count INTEGER NOT NULL,
			from_version INTEGER NOT NULL,
			to_version INTEGER NOT NULL,
			status TEXT NOT NULL,
			summary_json TEXT NOT NULL DEFAULT '{}',
			error_kind TEXT NOT NULL DEFAULT '',
			error_code TEXT NOT NULL DEFAULT '',
			error_message TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE TABLE IF NOT EXISTS decisions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			run_id TEXT NOT NULL REFERENCES replay_runs(run_id),
			version INTEGER NOT NULL,
			flow_key TEXT NOT NULL,
			flow_hash TEXT NOT NULL,
			member_id TEXT NOT NULL,
			reason TEXT NOT NULL,
			decided_at TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id, version)`,
		`CREATE TABLE IF NOT EXISTS migrations (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			run_id TEXT NOT NULL REFERENCES replay_runs(run_id),
			flow_key TEXT NOT NULL,
			flow_hash TEXT NOT NULL,
			old_member TEXT NOT NULL,
			new_member TEXT NOT NULL,
			reason TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_migrations_run ON migrations(run_id)`,
	}
	for _, st := range stmts {
		if _, err := s.db.ExecContext(ctx, st); err != nil {
			return mapErr(err)
		}
	}
	return nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the raw handle for the force-busy test helper only.
func (s *Store) DB() *sql.DB { return s.db }

// --- config revisions ------------------------------------------------------

type ConfigRevision struct {
	ID       int64
	LoadedAt time.Time
	Source   string
	SHA256   string
	Body     string
	Note     string
}

func (s *Store) InsertConfigRevision(ctx context.Context, rev ConfigRevision) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO config_revisions(loaded_at, source, sha256, body, note) VALUES(?,?,?,?,?)`,
		rev.LoadedAt.UTC().Format(time.RFC3339Nano), rev.Source, rev.SHA256, rev.Body, rev.Note)
	if err != nil {
		return 0, mapErr(err)
	}
	return res.LastInsertId()
}

// --- ring versions --------------------------------------------------------

type RingVersionRow struct {
	Version        int64
	CreatedAt      time.Time
	Change         string
	MembersJSON    []byte
	AllocationJSON []byte
	Fingerprint    string
}

func (s *Store) InsertRingVersion(ctx context.Context, row RingVersionRow) error {
	if json.Valid(row.MembersJSON) == false {
		return apperr.Invalid("BAD_JSON", "members_json is not valid JSON")
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO ring_versions(version, created_at, change, members_json, allocation_json, fingerprint)
		 VALUES(?,?,?,?,?,?)`,
		row.Version, row.CreatedAt.UTC().Format(time.RFC3339Nano), row.Change,
		string(row.MembersJSON), string(row.AllocationJSON), row.Fingerprint)
	if err != nil {
		return mapErr(err)
	}
	return nil
}

// RingVersion reads one persisted generation.
func (s *Store) RingVersion(ctx context.Context, version int64) (RingVersionRow, error) {
	var row RingVersionRow
	var created, members, alloc string
	err := s.db.QueryRowContext(ctx,
		`SELECT version, created_at, change, members_json, allocation_json, fingerprint
		 FROM ring_versions WHERE version = ?`, version).
		Scan(&row.Version, &created, &row.Change, &members, &alloc, &row.Fingerprint)
	if errors.Is(err, sql.ErrNoRows) {
		return row, apperr.Conflict("UNKNOWN_VERSION",
			fmt.Sprintf("ring version %d was never persisted", version))
	}
	if err != nil {
		return row, mapErr(err)
	}
	row.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	row.MembersJSON = []byte(members)
	row.AllocationJSON = []byte(alloc)
	return row, nil
}

func (s *Store) MaxRingVersion(ctx context.Context) (int64, error) {
	var v sql.NullInt64
	if err := s.db.QueryRowContext(ctx, `SELECT MAX(version) FROM ring_versions`).Scan(&v); err != nil {
		return 0, mapErr(err)
	}
	return v.Int64, nil
}

// --- replay runs -----------------------------------------------------------

type RunRow struct {
	RunID        string
	CreatedAt    time.Time
	FlowSetName  string
	FlowCount    int
	FromVersion  int64
	ToVersion    int64
	Status       string
	SummaryJSON  []byte
	ErrorKind    string
	ErrorCode    string
	ErrorMessage string
}

func (s *Store) InsertRun(ctx context.Context, row RunRow) error {
	summary := row.SummaryJSON
	if len(summary) == 0 {
		summary = []byte("{}")
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO replay_runs(run_id, created_at, flow_set_name, flow_count, from_version,
			to_version, status, summary_json, error_kind, error_code, error_message)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?)`,
		row.RunID, row.CreatedAt.UTC().Format(time.RFC3339Nano), row.FlowSetName, row.FlowCount,
		row.FromVersion, row.ToVersion, row.Status, string(summary),
		row.ErrorKind, row.ErrorCode, row.ErrorMessage)
	return mapErr(err)
}

func (s *Store) UpdateRunResult(ctx context.Context, runID, status string, summary []byte,
	errKind, errCode, errMessage string) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE replay_runs SET status=?, summary_json=?, error_kind=?, error_code=?, error_message=?
		 WHERE run_id=?`, status, string(summary), errKind, errCode, errMessage, runID)
	return mapErr(err)
}

type RunResult struct {
	RunID        string
	CreatedAt    time.Time
	FlowSetName  string
	FlowCount    int
	FromVersion  int64
	ToVersion    int64
	Status       string
	Summary      json.RawMessage
	ErrorKind    string
	ErrorCode    string
	ErrorMessage string
}

func (s *Store) GetRun(ctx context.Context, runID string) (RunResult, error) {
	var r RunResult
	var created, summary string
	err := s.db.QueryRowContext(ctx,
		`SELECT run_id, created_at, flow_set_name, flow_count, from_version, to_version,
		        status, summary_json, error_kind, error_code, error_message
		 FROM replay_runs WHERE run_id=?`, runID).
		Scan(&r.RunID, &created, &r.FlowSetName, &r.FlowCount, &r.FromVersion, &r.ToVersion,
			&r.Status, &summary, &r.ErrorKind, &r.ErrorCode, &r.ErrorMessage)
	if errors.Is(err, sql.ErrNoRows) {
		return r, apperr.Conflict("UNKNOWN_RUN", "no replay run with id "+runID)
	}
	if err != nil {
		return r, mapErr(err)
	}
	r.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	r.Summary = json.RawMessage(summary)
	return r, nil
}

// ListRuns returns run IDs newest first.
func (s *Store) ListRuns(ctx context.Context, limit int) ([]RunResult, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT run_id, created_at, flow_set_name, flow_count, from_version, to_version,
		        status, summary_json, error_kind, error_code, error_message
		 FROM replay_runs ORDER BY created_at DESC, rowid DESC LIMIT ?`, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	var out []RunResult
	for rows.Next() {
		var r RunResult
		var created, summary string
		if err := rows.Scan(&r.RunID, &created, &r.FlowSetName, &r.FlowCount, &r.FromVersion,
			&r.ToVersion, &r.Status, &summary, &r.ErrorKind, &r.ErrorCode, &r.ErrorMessage); err != nil {
			return nil, mapErr(err)
		}
		r.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
		r.Summary = json.RawMessage(summary)
		out = append(out, r)
	}
	return out, rows.Err()
}

// --- decisions & migrations ------------------------------------------------

type DecisionRow struct {
	RunID     string
	Version   int64
	FlowKey   string
	FlowHash  uint64
	MemberID  string
	Reason    string
	DecidedAt time.Time
}

// InsertDecisions bulk-inserts routing decisions for a run/version.
func (s *Store) InsertDecisions(ctx context.Context, rows []DecisionRow) error {
	if len(rows) == 0 {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return mapErr(err)
	}
	defer func() { _ = tx.Rollback() }()
	stmt, err := tx.PrepareContext(ctx,
		`INSERT INTO decisions(run_id, version, flow_key, flow_hash, member_id, reason, decided_at)
		 VALUES(?,?,?,?,?,?,?)`)
	if err != nil {
		return mapErr(err)
	}
	defer stmt.Close()
	for _, r := range rows {
		if _, err := stmt.ExecContext(ctx, r.RunID, r.Version, r.FlowKey,
			fmt.Sprintf("%d", r.FlowHash), r.MemberID, r.Reason,
			r.DecidedAt.UTC().Format(time.RFC3339Nano)); err != nil {
			return mapErr(err)
		}
	}
	return mapErr(tx.Commit())
}

type MigrationRow struct {
	RunID     string
	FlowKey   string
	FlowHash  uint64
	OldMember string
	NewMember string
	Reason    string
}

// InsertMigrations bulk-inserts the changed-owner records for a run.
func (s *Store) InsertMigrations(ctx context.Context, rows []MigrationRow) error {
	if len(rows) == 0 {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return mapErr(err)
	}
	defer func() { _ = tx.Rollback() }()
	stmt, err := tx.PrepareContext(ctx,
		`INSERT INTO migrations(run_id, flow_key, flow_hash, old_member, new_member, reason)
		 VALUES(?,?,?,?,?,?)`)
	if err != nil {
		return mapErr(err)
	}
	defer stmt.Close()
	for _, r := range rows {
		if _, err := stmt.ExecContext(ctx, r.RunID, r.FlowKey,
			fmt.Sprintf("%d", r.FlowHash), r.OldMember, r.NewMember, r.Reason); err != nil {
			return mapErr(err)
		}
	}
	return mapErr(tx.Commit())
}

// RunMigrations returns persisted owner changes for a run.
func (s *Store) RunMigrations(ctx context.Context, runID string) ([]MigrationRow, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT flow_key, flow_hash, old_member, new_member, reason
		 FROM migrations WHERE run_id=? ORDER BY id`, runID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	var out []MigrationRow
	for rows.Next() {
		var m MigrationRow
		var h string
		if err := rows.Scan(&m.FlowKey, &h, &m.OldMember, &m.NewMember, &m.Reason); err != nil {
			return nil, mapErr(err)
		}
		m.RunID = runID
		if _, err := fmt.Sscanf(h, "%d", &m.FlowHash); err != nil {
			return nil, apperr.Compute("BAD_HASH_IN_STORE", "stored flow hash is unparsable").WithCause(err)
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// mapErr normalizes driver errors onto the service taxonomy.
func mapErr(err error) error {
	if err == nil {
		return nil
	}
	var ae *apperr.Error
	if errors.As(err, &ae) {
		return err
	}
	msg := err.Error()
	if strings.Contains(msg, "SQLITE_BUSY") || strings.Contains(msg, "SQLITE_LOCKED") ||
		strings.Contains(msg, "database is locked") {
		return apperr.Exhausted("STORE_BUSY",
			"sqlite database is locked; retry after backoff").WithCause(err)
	}
	return apperr.Exhausted("STORE_IO", "sqlite failure: "+msg).WithCause(err)
}
