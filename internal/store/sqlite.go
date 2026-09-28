// Package store is the SQLite-backed persistence layer for custom resources.
// All writes to the resource table go through transactions and every mutating
// method is compare-and-swap on resource_version, so two writers (the HTTP
// API and the reconcile loop) can never silently clobber each other.
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

	"resourcecontroller/internal/model"
)

var (
	// ErrNotFound is returned when no row matches the identity key.
	ErrNotFound = errors.New("store: resource not found")
	// ErrVersionConflict is returned when a CAS write references a stale
	// resource_version. The caller is expected to reload and requeue rather
	// than overwrite.
	ErrVersionConflict = errors.New("store: resource version conflict")
	// ErrAlreadyExists is returned when creating a duplicate resource name.
	ErrAlreadyExists = errors.New("store: resource already exists")
)

// Store wraps the resource database.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the SQLite database at dsn and initializes
// the schema. Use an "file:...?_pragma=..." style DSN; see config defaults.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// modernc.org/sqlite serializes connections with a single open connection
	// and a busy timeout, which avoids SQLITE_BUSY under concurrent writers.
	db.SetMaxOpenConns(1)
	db.SetConnMaxIdleTime(0)
	s := &Store{db: db}
	if err := s.init(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) init(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, `
CREATE TABLE IF NOT EXISTS resources (
    name               TEXT PRIMARY KEY,
    uid                TEXT NOT NULL,
    generation         INTEGER NOT NULL,
    resource_version   INTEGER NOT NULL,
    deletion_timestamp TEXT,
    finalizers         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    spec               TEXT NOT NULL,
    status             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resources_pending
    ON resources(deletion_timestamp, generation, status);
`)
	return err
}

// Create inserts a new resource. Returns ErrAlreadyExists on duplicate name.
func (s *Store) Create(ctx context.Context, w *model.Widget) error {
	return s.mutate(ctx, func(tx *sql.Tx) error {
		var existing string
		err := tx.QueryRowContext(ctx, `SELECT name FROM resources WHERE name = ?`, w.Meta.Name).Scan(&existing)
		if err == nil {
			return ErrAlreadyExists
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		return insertOrReplace(ctx, tx, w)
	})
}

// Get reads a single resource by name.
func (s *Store) Get(ctx context.Context, name string) (*model.Widget, error) {
	var w model.Widget
	row := s.db.QueryRowContext(ctx, selectSQL+` WHERE name = ?`, name)
	if err := scanWidget(row.Scan, &w); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &w, nil
}

// List returns all resources ordered by name.
func (s *Store) List(ctx context.Context) ([]*model.Widget, error) {
	rows, err := s.db.QueryContext(ctx, selectSQL+` ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collect(ctx, rows)
}

// ListPending returns resources that still need reconcile work:
// those under deletion, or whose reconciled generation lags their desired
// generation. The reconcile loop uses this for periodic resync.
func (s *Store) ListPending(ctx context.Context) ([]*model.Widget, error) {
	rows, err := s.db.QueryContext(ctx,
		selectSQL+` WHERE deletion_timestamp IS NOT NULL
   OR json_extract(status, '$.reconciledGeneration') < generation
 ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collect(ctx, rows)
}

// SaveSpec performs a compare-and-swap update of spec/metadata. expectedRV
// must match the current resource_version. On conflict the row is untouched
// and ErrVersionConflict is returned. The controller treats that as
// "newer desired state exists; requeue, do not overwrite". It returns the
// resulting row on success.
func (s *Store) SaveSpec(ctx context.Context, w *model.Widget, expectedRV int64) (*model.Widget, error) {
	var out *model.Widget
	err := s.mutate(ctx, func(tx *sql.Tx) error {
		cur, err := loadForUpdate(ctx, tx, w.Meta.Name)
		if err != nil {
			return err
		}
		if cur.Meta.ResourceVersion != expectedRV {
			return ErrVersionConflict
		}
		// Preserve controller-owned status on spec writes; merge the two
		// generation bookkeeping fields only by max() so an API write can
		// never reset progress.
		w.Meta.ResourceVersion = cur.Meta.ResourceVersion + 1
		w.Meta.CreatedAt = cur.Meta.CreatedAt
		w.Meta.UpdatedAt = time.Now().UTC()
		if cur.Status.ObservedGeneration > w.Status.ObservedGeneration {
			w.Status.ObservedGeneration = cur.Status.ObservedGeneration
		}
		if cur.Status.ReconciledGeneration > w.Status.ReconciledGeneration {
			w.Status.ReconciledGeneration = cur.Status.ReconciledGeneration
		}
		if cur.Status.ExternalID != "" && w.Status.ExternalID == "" {
			w.Status.ExternalID = cur.Status.ExternalID
		}
		if cur.Status.ExternalVersion != 0 && w.Status.ExternalVersion == 0 {
			w.Status.ExternalVersion = cur.Status.ExternalVersion
		}
		if len(cur.Status.Conditions) > 0 && len(w.Status.Conditions) == 0 {
			w.Status.Conditions = cur.Status.Conditions
		}
		if cur.Status.LastAttempt != nil && w.Status.LastAttempt == nil {
			w.Status.LastAttempt = cur.Status.LastAttempt
		}
		if cur.Status.Phase != "" && w.Status.Phase == "" {
			w.Status.Phase = cur.Status.Phase
		}
		if err := insertOrReplace(ctx, tx, w); err != nil {
			return err
		}
		out = w
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// AddFinalizer ensures finalizer is present; the row is only rewritten when
// something actually changed.
func (s *Store) AddFinalizer(ctx context.Context, name, finalizer string, expectedRV int64) (*model.Widget, error) {
	var out *model.Widget
	err := s.mutate(ctx, func(tx *sql.Tx) error {
		w, err := loadForUpdate(ctx, tx, name)
		if err != nil {
			return err
		}
		if w.Meta.ResourceVersion != expectedRV {
			return ErrVersionConflict
		}
		for _, f := range w.Meta.Finalizers {
			if f == finalizer {
				out = w
				return nil
			}
		}
		w.Meta.Finalizers = append(w.Meta.Finalizers, finalizer)
		w.Meta.ResourceVersion++
		w.Meta.UpdatedAt = time.Now().UTC()
		if err := insertOrReplace(ctx, tx, w); err != nil {
			return err
		}
		out = w
		return nil
	})
	return out, err
}

// SaveStatus performs a compare-and-swap update of controller-owned status.
// Observed/reconciled generations move forward monotonically even if the
// reconcile loop passed a stale snapshot, so an old observation cannot move
// progress backwards. Spec and metadata apart from resource_version are
// never touched by status writes.
func (s *Store) SaveStatus(ctx context.Context, name string, st model.WidgetStatus, expectedRV int64) (*model.Widget, error) {
	var out *model.Widget
	err := s.mutate(ctx, func(tx *sql.Tx) error {
		w, err := loadForUpdate(ctx, tx, name)
		if err != nil {
			return err
		}
		if w.Meta.ResourceVersion != expectedRV {
			return ErrVersionConflict
		}
		if st.ObservedGeneration < w.Status.ObservedGeneration {
			st.ObservedGeneration = w.Status.ObservedGeneration
		}
		if st.ReconciledGeneration < w.Status.ReconciledGeneration {
			st.ReconciledGeneration = w.Status.ReconciledGeneration
		}
		w.Status = st
		w.Meta.ResourceVersion++
		w.Meta.UpdatedAt = time.Now().UTC()
		if err := insertOrReplace(ctx, tx, w); err != nil {
			return err
		}
		out = w
		return nil
	})
	return out, err
}

// Delete removes the record unconditionally. It is only called after the
// external cleanup has completed and the deletion finalizer is gone.
func (s *Store) Delete(ctx context.Context, name string, expectedRV int64) error {
	return s.mutate(ctx, func(tx *sql.Tx) error {
		var rv int64
		err := tx.QueryRowContext(ctx, `SELECT resource_version FROM resources WHERE name = ?`, name).Scan(&rv)
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if rv != expectedRV {
			return ErrVersionConflict
		}
		_, err = tx.ExecContext(ctx, `DELETE FROM resources WHERE name = ?`, name)
		return err
	})
}

// Mutate runs fn inside a single serialized transaction. Exported because the
// delete path needs finalizer removal and row delete to be atomic.
func (s *Store) Mutate(ctx context.Context, fn func(tx *sql.Tx) error) error {
	return s.mutate(ctx, fn)
}

// LoadTx reads a resource inside an existing transaction.
func LoadTx(ctx context.Context, tx *sql.Tx, name string) (*model.Widget, error) {
	return loadForUpdate(ctx, tx, name)
}

// DeleteTx deletes inside an existing transaction.
func DeleteTx(ctx context.Context, tx *sql.Tx, name string) error {
	res, err := tx.ExecContext(ctx, `DELETE FROM resources WHERE name = ?`, name)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) mutate(ctx context.Context, fn func(tx *sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit()
}

const selectSQL = `SELECT name, uid, generation, resource_version, deletion_timestamp,
       finalizers, created_at, updated_at, spec, status FROM resources`

type rowScanner func(dest ...any) error

func scanWidget(scan rowScanner, w *model.Widget) error {
	var deletionTS sql.NullString
	var finalizers, specJSON, statusJSON string
	var createdAt, updatedAt string
	err := scan(
		&w.Meta.Name, &w.Meta.UID, &w.Meta.Generation, &w.Meta.ResourceVersion,
		&deletionTS, &finalizers, &createdAt, &updatedAt,
		&specJSON, &statusJSON,
	)
	if err != nil {
		return err
	}
	if w.Meta.CreatedAt, err = time.Parse(time.RFC3339Nano, createdAt); err != nil {
		return fmt.Errorf("parsing created_at: %w", err)
	}
	if w.Meta.UpdatedAt, err = time.Parse(time.RFC3339Nano, updatedAt); err != nil {
		return fmt.Errorf("parsing updated_at: %w", err)
	}
	if deletionTS.Valid && deletionTS.String != "" {
		t, err := time.Parse(time.RFC3339Nano, deletionTS.String)
		if err != nil {
			return fmt.Errorf("parsing deletion timestamp: %w", err)
		}
		w.Meta.DeletionTimestamp = &t
	}
	if err := json.Unmarshal([]byte(finalizers), &w.Meta.Finalizers); err != nil {
		return fmt.Errorf("parsing finalizers: %w", err)
	}
	if err := json.Unmarshal([]byte(specJSON), &w.Spec); err != nil {
		return fmt.Errorf("parsing spec: %w", err)
	}
	if err := json.Unmarshal([]byte(statusJSON), &w.Status); err != nil {
		return fmt.Errorf("parsing status: %w", err)
	}
	return nil
}

func collect(ctx context.Context, rows *sql.Rows) ([]*model.Widget, error) {
	var out []*model.Widget
	for rows.Next() {
		var w model.Widget
		if err := scanWidget(rows.Scan, &w); err != nil {
			return nil, err
		}
		out = append(out, &w)
	}
	return out, rows.Err()
}

func loadForUpdate(ctx context.Context, tx *sql.Tx, name string) (*model.Widget, error) {
	var w model.Widget
	err := scanWidget(tx.QueryRowContext(ctx, selectSQL+` WHERE name = ?`, name).Scan, &w)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &w, nil
}

func insertOrReplace(ctx context.Context, tx *sql.Tx, w *model.Widget) error {
	specJSON, err := json.Marshal(w.Spec)
	if err != nil {
		return err
	}
	statusJSON, err := json.Marshal(w.Status)
	if err != nil {
		return err
	}
	finalizers := w.Meta.Finalizers
	if finalizers == nil {
		finalizers = []string{}
	}
	finJSON, err := json.Marshal(finalizers)
	if err != nil {
		return err
	}
	var deletionTS any
	if w.Meta.DeletionTimestamp != nil {
		deletionTS = w.Meta.DeletionTimestamp.UTC().Format(time.RFC3339Nano)
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO resources (
        name, uid, generation, resource_version, deletion_timestamp,
        finalizers, created_at, updated_at, spec, status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
        uid=excluded.uid,
        generation=excluded.generation,
        resource_version=excluded.resource_version,
        deletion_timestamp=excluded.deletion_timestamp,
        finalizers=excluded.finalizers,
        created_at=excluded.created_at,
        updated_at=excluded.updated_at,
        spec=excluded.spec,
        status=excluded.status`,
		w.Meta.Name, w.Meta.UID, w.Meta.Generation, w.Meta.ResourceVersion,
		deletionTS, string(finJSON),
		w.Meta.CreatedAt.UTC().Format(time.RFC3339Nano),
		w.Meta.UpdatedAt.UTC().Format(time.RFC3339Nano),
		string(specJSON), string(statusJSON),
	)
	return err
}

// QueryExists is a small helper used in tests.
func (s *Store) QueryExists(ctx context.Context, name string) (bool, error) {
	var n int
	err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM resources WHERE name = ?`, name).Scan(&n)
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

// DescribeError classifies a store error for diagnostics.
func DescribeError(err error) string {
	switch {
	case err == nil:
		return ""
	case errors.Is(err, ErrNotFound):
		return "not-found"
	case errors.Is(err, ErrVersionConflict):
		return "version-conflict"
	case errors.Is(err, ErrAlreadyExists):
		return "already-exists"
	case strings.Contains(err.Error(), "SQLITE_BUSY"):
		return "database-busy"
	default:
		return "storage-error"
	}
}
