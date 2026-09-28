// Package actualstore persists the physical resources owned by the actual
// resource service. It uses its own SQLite file, independent of the
// desired-plane database: this separation is what lets fault injection and
// independent verification observe reality without trusting the controller.
package actualstore

import (
	"database/sql"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"
)

var (
	// ErrNotFound means no physical resource matched.
	ErrNotFound = errors.New("actual resource not found")
	// ErrAlreadyOwned is the idempotent-create conflict: an owner UID already
	// has a resource. The client claims the returned resource instead of
	// creating a second one.
	ErrAlreadyOwned = errors.New("owner already has resource")
	// ErrVersionConflict is a failed conditional update/delete.
	ErrVersionConflict = errors.New("actual resource version conflict")
)

// Store is the actual-plane persistence layer.
type Store struct {
	db *sql.DB
}

// Open initializes the actual-plane database.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	for _, p := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA busy_timeout=5000",
		"PRAGMA foreign_keys=ON",
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
CREATE TABLE IF NOT EXISTS actual_resources (
    id            TEXT PRIMARY KEY,
    owner_uid     TEXT NOT NULL UNIQUE,
    generation    INTEGER NOT NULL,
    spec_hash     TEXT NOT NULL,
    spec          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    fault       TEXT NOT NULL DEFAULT '',
    body_redacted TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reqlog_req ON request_log(request_id, seq);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
`

// row is the stored representation of a physical resource.
type row struct {
	ID         string
	OwnerUID   string
	Generation int64
	SpecHash   string
	Spec       string
	Version    int64
	State      string
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

// Resource is a physical resource as handled inside the service.
type Resource struct {
	ID         string         `json:"id"`
	OwnerUID   string         `json:"ownerUID"`
	Generation int64          `json:"generation"`
	SpecHash   string         `json:"specHash"`
	Spec       map[string]any `json:"spec"`
	Version    int64          `json:"version"`
	State      string         `json:"state"`
	CreatedAt  time.Time      `json:"createdAt"`
	UpdatedAt  time.Time      `json:"updatedAt"`
}

const resCols = `id, owner_uid, generation, spec_hash, spec, version, state,
    created_at, updated_at`

func scanRow(s interface {
	Scan(dest ...any) error
}) (*Resource, error) {
	var r row
	var created, updated string
	err := s.Scan(&r.ID, &r.OwnerUID, &r.Generation, &r.SpecHash, &r.Spec,
		&r.Version, &r.State, &created, &updated)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	out := &Resource{
		ID: r.ID, OwnerUID: r.OwnerUID, Generation: r.Generation,
		SpecHash: r.SpecHash, Version: r.Version, State: r.State,
		Spec: map[string]any{},
	}
	_ = decodeJSON(r.Spec, &out.Spec)
	out.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	out.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	return out, nil
}

// Get fetches a resource by physical id.
func (s *Store) Get(id string) (*Resource, error) {
	return scanRow(s.db.QueryRow(
		`SELECT `+resCols+` FROM actual_resources WHERE id = ?`, id))
}

// GetByOwner fetches a resource by owner UID (claim path after a lost create).
func (s *Store) GetByOwner(ownerUID string) (*Resource, error) {
	return scanRow(s.db.QueryRow(
		`SELECT `+resCols+` FROM actual_resources WHERE owner_uid = ?`, ownerUID))
}

// List returns up to limit physical resources (independent verification
// oracle: count real resources regardless of what the controller believes).
func (s *Store) List(limit int) ([]*Resource, error) {
	rows, err := s.db.Query(
		`SELECT `+resCols+` FROM actual_resources ORDER BY id LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Resource
	for rows.Next() {
		r, err := scanRow(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// CreateInput describes a new physical resource.
type CreateInput struct {
	ID         string
	OwnerUID   string
	Generation int64
	SpecHash   string
	Spec       map[string]any
	State      string
}

// Create inserts a resource. If the owner already owns one, it returns that
// resource together with ErrAlreadyOwned; no duplicate row is ever created.
func (s *Store) Create(in CreateInput) (*Resource, error) {
	ts := time.Now().UTC()
	if in.State == "" {
		in.State = "Active"
	}
	res, err := s.db.Exec(`
INSERT INTO actual_resources(id, owner_uid, generation, spec_hash, spec,
    version, state, created_at, updated_at)
VALUES(?,?,?,?,?,1,?,?,?)`,
		in.ID, in.OwnerUID, in.Generation, in.SpecHash, encodeJSON(in.Spec),
		in.State, ts.Format(time.RFC3339Nano), ts.Format(time.RFC3339Nano))
	if err != nil {
		if isUnique(err) {
			existing, gerr := s.GetByOwner(in.OwnerUID)
			if gerr != nil {
				return nil, gerr
			}
			return existing, ErrAlreadyOwned
		}
		return nil, err
	}
	_ = res
	return s.Get(in.ID)
}

// UpdateInput is a conditional update.
type UpdateInput struct {
	ID          string
	ExpectedVer int64
	Generation  int64
	SpecHash    string
	Spec        map[string]any
}

// Update applies a conditional spec update. On success the version is
// incremented; the prior values are returned so the fault-injection layer can
// serve a deliberately stale snapshot afterwards.
func (s *Store) Update(in UpdateInput) (*Resource, error) {
	ts := time.Now().UTC()
	res, err := s.db.Exec(`
UPDATE actual_resources
SET generation = ?, spec_hash = ?, spec = ?,
    version = version + 1, updated_at = ?
WHERE id = ? AND version = ?`,
		in.Generation, in.SpecHash, encodeJSON(in.Spec),
		ts.Format(time.RFC3339Nano), in.ID, in.ExpectedVer)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		if _, gerr := s.Get(in.ID); gerr != nil {
			return nil, gerr
		}
		return nil, ErrVersionConflict
	}
	return s.Get(in.ID)
}

// Delete removes a resource, guarded by version. expectedVer <= 0 means
// unconditional (used by the controller once ownership is known).
func (s *Store) Delete(id string, expectedVer int64) error {
	var (
		res sql.Result
		err error
	)
	if expectedVer > 0 {
		res, err = s.db.Exec(
			`DELETE FROM actual_resources WHERE id = ? AND version = ?`,
			id, expectedVer)
	} else {
		res, err = s.db.Exec(
			`DELETE FROM actual_resources WHERE id = ?`, id)
	}
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		if _, gerr := s.Get(id); gerr != nil {
			return ErrNotFound
		}
		return ErrVersionConflict
	}
	return nil
}
