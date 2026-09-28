// Package store persists resources, field ownership, apply history and kind
// schemas in SQLite. The central invariant enforced here: a merged value,
// its ownership set and a history row are written in ONE transaction, so no
// reader can ever observe a value whose ownership disagrees with it.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"

	"fieldmerge/internal/apperr"
	"fieldmerge/internal/merge"
	"fieldmerge/internal/schema"
)

// Limits guards resource consumption at the storage boundary.
type Limits struct {
	MaxResources int // 0 disables the cap
	MaxPayload   int // max live JSON size in bytes; 0 disables
}

// DefaultLimits are applied when Open is given zero-valued Limits.
var DefaultLimits = Limits{MaxResources: 10000, MaxPayload: 1 << 20 /* 1 MiB */}

type Store struct {
	db      *sql.DB
	mu      sync.Mutex // serializes writers (SQLite single-writer)
	limits  Limits
	nowFunc func() time.Time
}

const ddl = `
CREATE TABLE IF NOT EXISTS resources (
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    live        TEXT NOT NULL DEFAULT '{}',
    revision    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    reconcile_at REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);
CREATE TABLE IF NOT EXISTS ownership (
    kind     TEXT NOT NULL,
    name     TEXT NOT NULL,
    path     TEXT NOT NULL,
    managers TEXT NOT NULL,
    rev      INTEGER NOT NULL,
    PRIMARY KEY (kind, name, path)
);
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    name      TEXT NOT NULL,
    rev       INTEGER NOT NULL,
    manager   TEXT NOT NULL,
    forced    INTEGER NOT NULL,
    config    TEXT NOT NULL,
    live      TEXT NOT NULL,
    changes   TEXT NOT NULL,
    pruned    TEXT NOT NULL,
    run_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_resource ON history(kind, name, rev);
CREATE TABLE IF NOT EXISTS schemas (
    kind       TEXT PRIMARY KEY,
    decl       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
`

// Open opens (and migrates) the database at dsn ("file:..." path or
// ":memory:").
func Open(dsn string, lim Limits) (*Store, error) {
	if lim == (Limits{}) {
		lim = DefaultLimits
	}
	// _txlock=immediate makes writers fail fast instead of silently
	// starting deferred transactions that upgrade to write locks.
	connStr := dsn
	if !strings.Contains(connStr, "?") {
		connStr += "?_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)&_txlock=immediate"
	}
	db, err := sql.Open("sqlite", connStr)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_open", "open sqlite: %v", err)
	}
	db.SetMaxOpenConns(1) // all access serialized; avoids lock surprises
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, apperr.New(apperr.Internal, "db_open", "ping sqlite: %v", err)
	}
	if _, err := db.Exec(ddl); err != nil {
		_ = db.Close()
		return nil, apperr.New(apperr.Internal, "db_migrate", "schema migration failed: %v", err)
	}
	return &Store{db: db, limits: lim, nowFunc: func() time.Time { return time.Now().UTC() }}, nil
}

func (s *Store) Close() error { return s.db.Close() }

// DB exposes the handle for tests that need to corrupt rows on purpose.
func (s *Store) DB() *sql.DB { return s.db }

func (s *Store) now() time.Time { return s.nowFunc() }

// Resource is one stored live object plus reconciliation metadata.
type Resource struct {
	Kind        string          `json:"kind"`
	Name        string          `json:"name"`
	Live        json.RawMessage `json:"live"`
	Revision    int64           `json:"revision"`
	Status      string          `json:"status"`
	LastError   string          `json:"last_error"`
	Attempts    int             `json:"attempts"`
	ReconcileAt time.Time       `json:"reconcile_at"`
	UpdatedAt   time.Time       `json:"updated_at"`
}

// HistoryEntry is one auditable apply result.
type HistoryEntry struct {
	ID        int64           `json:"id"`
	Kind      string          `json:"kind"`
	Name      string          `json:"name"`
	Revision  int64           `json:"revision"`
	Manager   string          `json:"manager"`
	Forced    bool            `json:"forced"`
	Config    json.RawMessage `json:"config"`
	Live      json.RawMessage `json:"live"`
	Changes   json.RawMessage `json:"changes"`
	Pruned    json.RawMessage `json:"pruned"`
	RunID     string          `json:"run_id"`
	CreatedAt time.Time       `json:"created_at"`
}

// ApplyRequest is the store-level apply input.
type ApplyRequest struct {
	Kind    string
	Name    string
	Manager string
	Force   bool
	Config  json.RawMessage
	Schema  *schema.Schema
	RunID   string
}

// ApplyOutcome mirrors merge.Result plus the new revision and persisted live
// bytes.
type ApplyOutcome struct {
	Revision int64
	Live     json.RawMessage
	Result   *merge.Result
}

// ErrConflictApply is returned (wrapped in apperr) when the engine reports
// conflicts — nothing is persisted.
type conflictApplyError struct {
	result *merge.Result
}

func (e *conflictApplyError) Error() string { return "field ownership conflicts" }

// ConflictResult extracts the diagnostic result from an apply that failed with
// ownership conflicts.
func ConflictResult(err error) (*merge.Result, bool) {
	var ce *conflictApplyError
	if errors.As(err, &ce) {
		return ce.result, true
	}
	return nil, false
}

// Apply runs the merge and persists value + ownership + history atomically.
//
// Serialized with a process-level mutex because SQLite permits one writer.
func (s *Store) Apply(ctx context.Context, req ApplyRequest) (*ApplyOutcome, error) {
	if req.Kind == "" || req.Name == "" {
		return nil, apperr.New(apperr.InvalidInput, "resource_id_required", "kind and name are required")
	}
	if req.Manager == "" {
		return nil, apperr.New(apperr.InvalidInput, "manager_required", "manager must be non-empty")
	}
	if s.limits.MaxPayload > 0 && len(req.Config) > s.limits.MaxPayload {
		return nil, apperr.New(apperr.ResourceExhausted, "payload_too_large",
			"config payload %d bytes exceeds limit %d", len(req.Config), s.limits.MaxPayload)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "tx_begin", "begin tx: %v", err)
	}
	defer func() { _ = tx.Rollback() }()

	live := json.RawMessage(`{}`)
	var liveS string
	var rev int64
	var exists bool
	row := tx.QueryRowContext(ctx,
		`SELECT live, revision FROM resources WHERE kind=? AND name=?`, req.Kind, req.Name)
	if err := row.Scan(&liveS, &rev); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			if s.limits.MaxResources > 0 {
				var n int
				if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM resources`).Scan(&n); err != nil {
					return nil, apperr.New(apperr.Internal, "db_count", "count resources: %v", err)
				}
				if n >= s.limits.MaxResources {
					return nil, apperr.New(apperr.ResourceExhausted, "resource_limit",
						"resource count %d reaches limit %d", n, s.limits.MaxResources)
				}
			}
		} else {
			return nil, apperr.New(apperr.Internal, "db_read", "read resource: %v", err)
		}
	} else {
		live = json.RawMessage(liveS)
		exists = true
	}

	var claims []merge.Claim
	rows, err := tx.QueryContext(ctx,
		`SELECT path, managers FROM ownership WHERE kind=? AND name=? ORDER BY path`,
		req.Kind, req.Name)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "read ownership: %v", err)
	}
	for rows.Next() {
		var path, mgrsJSON string
		if err := rows.Scan(&path, &mgrsJSON); err != nil {
			rows.Close()
			return nil, apperr.New(apperr.Internal, "db_read", "scan ownership: %v", err)
		}
		var mgrs []string
		if err := json.Unmarshal([]byte(mgrsJSON), &mgrs); err != nil {
			rows.Close()
			return nil, apperr.New(apperr.Internal, "ownership_corrupt",
				"stored ownership JSON at %s invalid: %v", path, err)
		}
		claims = append(claims, merge.Claim{Path: path, Managers: mgrs})
	}
	rows.Close()

	result, err := merge.Apply(merge.Input{
		Kind: req.Kind, Name: req.Name, Manager: req.Manager, Force: req.Force,
		Live: live, Config: req.Config, Schema: req.Schema,
	}, claims)
	if err != nil {
		return nil, err
	}
	if len(result.Conflict) > 0 {
		return nil, &conflictApplyError{result: result}
	}

	newLive, err := json.Marshal(result.Live)
	if err != nil {
		return nil, apperr.New(apperr.ComputeFailure, "live_encode", "encode merged live: %v", err)
	}
	if s.limits.MaxPayload > 0 && len(newLive) > s.limits.MaxPayload {
		return nil, apperr.New(apperr.ResourceExhausted, "live_too_large",
			"merged live %d bytes exceeds limit %d", len(newLive), s.limits.MaxPayload)
	}
	changes, _ := json.Marshal(result.Changes)
	pruned, _ := json.Marshal(result.PrunedOwnership)
	now := s.now()
	newRev := rev + 1

	if exists {
		_, err = tx.ExecContext(ctx, `
UPDATE resources SET live=?, revision=?, status='pending', attempts=0,
       last_error='', reconcile_at=0, updated_at=?
WHERE kind=? AND name=?`,
			string(newLive), newRev, now.Format(time.RFC3339Nano), req.Kind, req.Name)
	} else {
		_, err = tx.ExecContext(ctx, `
INSERT INTO resources(kind,name,live,revision,status,last_error,attempts,reconcile_at,updated_at)
VALUES(?,?,?,?,'pending','',0,0,?)`,
			req.Kind, req.Name, string(newLive), newRev, now.Format(time.RFC3339Nano))
	}
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_write", "write resource: %v", err)
	}

	if _, err := tx.ExecContext(ctx,
		`DELETE FROM ownership WHERE kind=? AND name=?`, req.Kind, req.Name); err != nil {
		return nil, apperr.New(apperr.Internal, "db_write", "clear ownership: %v", err)
	}
	for _, c := range result.Claims {
		mgrs, _ := json.Marshal(c.Managers)
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO ownership(kind,name,path,managers,rev) VALUES(?,?,?,?,?)`,
			req.Kind, req.Name, c.Path, string(mgrs), newRev); err != nil {
			return nil, apperr.New(apperr.Internal, "db_write", "write ownership: %v", err)
		}
	}

	if _, err := tx.ExecContext(ctx, `
INSERT INTO history(kind,name,rev,manager,forced,config,live,changes,pruned,run_id,created_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?)`,
		req.Kind, req.Name, newRev, req.Manager, boolInt(req.Force),
		string(req.Config), string(newLive), string(changes), string(pruned),
		req.RunID, now.Format(time.RFC3339Nano)); err != nil {
		return nil, apperr.New(apperr.Internal, "db_write", "write history: %v", err)
	}

	if err := tx.Commit(); err != nil {
		return nil, apperr.New(apperr.Internal, "tx_commit", "commit: %v", err)
	}
	return &ApplyOutcome{Revision: newRev, Live: newLive, Result: result}, nil
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// Get fetches one resource (nil, nil when missing).
func (s *Store) Get(ctx context.Context, kind, name string) (*Resource, error) {
	var r Resource
	var updated string
	var reconcileAt sql.NullFloat64
	var live string
	err := s.db.QueryRowContext(ctx,
		`SELECT kind,name,live,revision,status,last_error,attempts,reconcile_at,updated_at
		 FROM resources WHERE kind=? AND name=?`, kind, name).
		Scan(&r.Kind, &r.Name, &live, &r.Revision, &r.Status, &r.LastError,
			&r.Attempts, &reconcileAt, &updated)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "read resource: %v", err)
	}
	r.Live = json.RawMessage(live)
	r.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	if reconcileAt.Valid && reconcileAt.Float64 > 0 {
		r.ReconcileAt = time.Unix(int64(reconcileAt.Float64), 0).UTC()
	}
	return &r, nil
}

// Ownership returns the claim set of a resource, ordered by path.
func (s *Store) Ownership(ctx context.Context, kind, name string) ([]merge.Claim, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT path, managers FROM ownership WHERE kind=? AND name=? ORDER BY path`, kind, name)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "read ownership: %v", err)
	}
	defer rows.Close()
	var out []merge.Claim
	for rows.Next() {
		var path, mgrsJSON string
		if err := rows.Scan(&path, &mgrsJSON); err != nil {
			return nil, apperr.New(apperr.Internal, "db_read", "scan ownership: %v", err)
		}
		var mgrs []string
		if err := json.Unmarshal([]byte(mgrsJSON), &mgrs); err != nil {
			return nil, apperr.New(apperr.Internal, "ownership_corrupt",
				"stored ownership JSON at %s invalid: %v", path, err)
		}
		out = append(out, merge.Claim{Path: path, Managers: mgrs})
	}
	return out, nil
}

// History returns apply history newest-first, newest revision first.
func (s *Store) History(ctx context.Context, kind, name string, limit int) ([]HistoryEntry, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT id,kind,name,rev,manager,forced,config,live,changes,pruned,run_id,created_at
		 FROM history WHERE kind=? AND name=? ORDER BY rev DESC, id DESC LIMIT ?`,
		kind, name, limit)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "read history: %v", err)
	}
	defer rows.Close()
	var out []HistoryEntry
	for rows.Next() {
		var h HistoryEntry
		var forced int
		var config, live, changes, pruned, created string
		if err := rows.Scan(&h.ID, &h.Kind, &h.Name, &h.Revision, &h.Manager, &forced,
			&config, &live, &changes, &pruned, &h.RunID, &created); err != nil {
			return nil, apperr.New(apperr.Internal, "db_read", "scan history: %v", err)
		}
		h.Forced = forced == 1
		h.Config = json.RawMessage(config)
		h.Live = json.RawMessage(live)
		h.Changes = json.RawMessage(changes)
		h.Pruned = json.RawMessage(pruned)
		h.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
		out = append(out, h)
	}
	return out, nil
}

// ResourceRef is a list item.
type ResourceRef struct {
	Kind      string    `json:"kind"`
	Name      string    `json:"name"`
	Revision  int64     `json:"revision"`
	Status    string    `json:"status"`
	UpdatedAt time.Time `json:"updated_at"`
}

// List lists resources, optionally filtered by kind.
func (s *Store) List(ctx context.Context, kind string) ([]ResourceRef, error) {
	q := `SELECT kind,name,revision,status,updated_at FROM resources`
	args := []any{}
	if kind != "" {
		q += ` WHERE kind=?`
		args = append(args, kind)
	}
	q += ` ORDER BY kind,name`
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "list resources: %v", err)
	}
	defer rows.Close()
	var out []ResourceRef
	for rows.Next() {
		var r ResourceRef
		var updated string
		if err := rows.Scan(&r.Kind, &r.Name, &r.Revision, &r.Status, &updated); err != nil {
			return nil, apperr.New(apperr.Internal, "db_read", "scan resource: %v", err)
		}
		r.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
		out = append(out, r)
	}
	return out, nil
}

// ReconcileResult is what the adapter reports back.
type ReconcileResult struct {
	Status    string // "synced" | "failed"
	Error     string
	NextRetry time.Time
}

// MarkReconcile updates reconciliation metadata under the write lock.
func (s *Store) MarkReconcile(ctx context.Context, kind, name string, res ReconcileResult) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var next float64
	if !res.NextRetry.IsZero() {
		next = float64(res.NextRetry.Unix())
	}
	_, err := s.db.ExecContext(ctx, `
UPDATE resources SET status=?, last_error=?,
       attempts = CASE WHEN ?='failed' THEN attempts+1 ELSE 0 END,
       reconcile_at=?
WHERE kind=? AND name=?`,
		res.Status, res.Error, res.Status, next, kind, name)
	if err != nil {
		return apperr.New(apperr.Internal, "db_write", "mark reconcile: %v", err)
	}
	return nil
}

// DueResources returns resources whose reconcile_at has passed and that are
// not already synced, ordered by due time.
func (s *Store) DueResources(ctx context.Context, now time.Time, limit int) ([]Resource, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT kind,name,live,revision,status,last_error,attempts,updated_at
FROM resources
WHERE status != 'synced' AND reconcile_at <= ?
ORDER BY reconcile_at LIMIT ?`, float64(now.Unix()), limit)
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "due resources: %v", err)
	}
	defer rows.Close()
	var out []Resource
	for rows.Next() {
		var r Resource
		var live, updated string
		if err := rows.Scan(&r.Kind, &r.Name, &live, &r.Revision, &r.Status,
			&r.LastError, &r.Attempts, &updated); err != nil {
			return nil, apperr.New(apperr.Internal, "db_read", "scan due: %v", err)
		}
		r.Live = json.RawMessage(live)
		r.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
		out = append(out, r)
	}
	return out, nil
}

// ---- schemas ----

// PutSchema validates and persists a kind schema.
func (s *Store) PutSchema(ctx context.Context, sc *schema.Schema) error {
	body, err := json.Marshal(sc.Lists)
	if err != nil {
		return apperr.New(apperr.InvalidInput, "schema_bad", "encode schema: %v", err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO schemas(kind,decl,created_at) VALUES(?,?,?)
		 ON CONFLICT(kind) DO UPDATE SET decl=excluded.decl`,
		sc.Kind, string(body), s.now().Format(time.RFC3339Nano))
	if err != nil {
		return apperr.New(apperr.Internal, "db_write", "write schema: %v", err)
	}
	return nil
}

// GetSchema loads a kind schema. A missing schema is represented by an empty
// (non-nil) Schema so resources with no lists still work.
func (s *Store) GetSchema(ctx context.Context, kind string) (*schema.Schema, error) {
	var body string
	err := s.db.QueryRowContext(ctx, `SELECT decl FROM schemas WHERE kind=?`, kind).Scan(&body)
	if errors.Is(err, sql.ErrNoRows) {
		return &schema.Schema{Kind: kind, Lists: map[string]schema.ListDecl{}}, nil
	}
	if err != nil {
		return nil, apperr.New(apperr.Internal, "db_read", "read schema: %v", err)
	}
	decls := map[string]schema.ListDecl{}
	if err := json.Unmarshal([]byte(body), &decls); err != nil {
		return nil, apperr.New(apperr.Internal, "schema_corrupt", "stored schema invalid: %v", err)
	}
	return &schema.Schema{Kind: kind, Lists: decls}, nil
}
