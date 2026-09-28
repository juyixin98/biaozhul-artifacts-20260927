package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"crcontroller/internal/model"
)

// ErrNotFound is returned when no row matches.
var ErrNotFound = errors.New("resource not found")

// ErrConflict is returned when an optimistic-concurrency precondition fails.
var ErrConflict = errors.New("resource version conflict")

// ErrRefused is returned for semantically invalid writes (e.g. observed
// generation ahead of the desired generation).
var ErrRefused = errors.New("write refused")

// ErrTerminating is returned when a spec update targets a deleting object.
var ErrTerminating = errors.New("object is terminating")

// now is overridable in tests.
var now = time.Now

// rowScanner abstracts *sql.Row / *sql.Rows.
type rowScanner interface {
	Scan(dest ...any) error
}

func scanObject(row rowScanner) (*model.Object, error) {
	var (
		o          model.Object
		specJSON   string
		finalizers string
		statusJSON string
		deletion   sql.NullString
		annotJSON  string
		createdAt  string
		updatedAt  string
	)
	err := row.Scan(
		&o.UID, &o.Namespace, &o.Name, &o.Generation, &o.ResourceVer,
		&specJSON, &o.SpecHash, &finalizers, &deletion,
		&statusJSON, &annotJSON, &createdAt, &updatedAt,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if err := decodeJSON(specJSON, &o.Spec); err != nil {
		return nil, fmt.Errorf("decode spec: %w", err)
	}
	if err := decodeJSON(finalizers, &o.Finalizers); err != nil {
		return nil, fmt.Errorf("decode finalizers: %w", err)
	}
	if err := decodeJSON(statusJSON, &o.Status); err != nil {
		return nil, fmt.Errorf("decode status: %w", err)
	}
	if err := decodeJSON(annotJSON, &o.Annotations); err != nil {
		return nil, fmt.Errorf("decode annotations: %w", err)
	}
	if deletion.Valid && deletion.String != "" {
		if ts, perr := time.Parse(time.RFC3339Nano, deletion.String); perr == nil {
			o.DeletionTS = &ts
		}
	}
	o.CreatedAt, _ = time.Parse(time.RFC3339Nano, createdAt)
	o.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updatedAt)
	return &o, nil
}

const objectCols = `uid, namespace, name, generation, resource_version,
    spec, spec_hash, finalizers, deletion_ts, status, annotations,
    created_at, updated_at`

// Get fetches an object by UID.
func (s *Store) Get(ctx context.Context, uid string) (*model.Object, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT `+objectCols+` FROM resources WHERE uid = ?`, uid)
	return scanObject(row)
}

// GetByName fetches an object by namespace/name.
func (s *Store) GetByName(ctx context.Context, namespace, name string) (*model.Object, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT `+objectCols+` FROM resources WHERE namespace = ? AND name = ?`,
		namespace, name)
	return scanObject(row)
}

// List returns up to limit objects, optionally including terminating ones.
func (s *Store) List(ctx context.Context, includeTerminating bool, limit int) ([]*model.Object, error) {
	q := `SELECT ` + objectCols + ` FROM resources`
	if !includeTerminating {
		q += ` WHERE deletion_ts IS NULL`
	}
	q += ` ORDER BY uid LIMIT ?`
	rows, err := s.db.QueryContext(ctx, q, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Object
	for rows.Next() {
		o, err := scanObject(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// CreateInput carries a new resource.
type CreateInput struct {
	UID         string
	Namespace   string
	Name        string
	Spec        map[string]any
	Annotations map[string]string
}

// Create inserts a new object at generation 1 with empty status. It fails if
// namespace/name is already taken.
func (s *Store) Create(ctx context.Context, in CreateInput) (*model.Object, error) {
	ts := now().UTC()
	o := &model.Object{
		UID:         in.UID,
		Namespace:   in.Namespace,
		Name:        in.Name,
		Generation:  1,
		ResourceVer: 1,
		Spec:        in.Spec,
		Finalizers:  []string{},
		Status: model.Status{
			ObservedGeneration: 0,
			Conditions:         []model.Condition{},
		},
		Annotations: in.Annotations,
		CreatedAt:   ts,
		UpdatedAt:   ts,
	}
	if o.Spec == nil {
		o.Spec = map[string]any{}
	}
	if o.Annotations == nil {
		o.Annotations = map[string]string{}
	}
	o.SpecHash = hashSpec(o.Spec)
	_, err := s.db.ExecContext(ctx, `
INSERT INTO resources(uid, namespace, name, generation, resource_version,
    spec, spec_hash, finalizers, deletion_ts, status, annotations,
    created_at, updated_at)
VALUES(?,?,?,?,1,?,?,'[]',NULL,?,?,?,?)`,
		o.UID, o.Namespace, o.Name, o.Generation,
		encodeJSON(o.Spec), o.SpecHash,
		encodeJSON(o.Status), encodeJSON(o.Annotations),
		ts.Format(time.RFC3339Nano), ts.Format(time.RFC3339Nano))
	if err != nil {
		if isUnique(err) {
			return nil, ErrConflict
		}
		return nil, err
	}
	return o, nil
}

// UpdateSpecInput replaces the spec. If the hash is unchanged, generation is
// NOT bumped (no-op spec writes still bump resource_version as an
// acknowledgement of the write).
type UpdateSpecInput struct {
	UID             string
	ResourceVersion int64
	Spec            map[string]any
}

// UpdateSpec applies an optimistic spec patch.
func (s *Store) UpdateSpec(ctx context.Context, in UpdateSpecInput) (*model.Object, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	o, err := getForUpdate(ctx, tx, in.UID)
	if err != nil {
		return nil, err
	}
	if o.DeletionTS != nil {
		return nil, ErrTerminating
	}
	if o.ResourceVer != in.ResourceVersion {
		return nil, ErrConflict
	}
	newHash := hashSpec(in.Spec)
	ts := now().UTC()
	var gen int64
	if newHash != o.SpecHash {
		gen = o.Generation + 1
	} else {
		gen = o.Generation
	}
	res, err := tx.ExecContext(ctx, `
UPDATE resources SET spec = ?, spec_hash = ?, generation = ?,
    resource_version = resource_version + 1, updated_at = ?
WHERE uid = ? AND resource_version = ? AND deletion_ts IS NULL`,
		encodeJSON(in.Spec), newHash, gen, ts.Format(time.RFC3339Nano),
		in.UID, in.ResourceVersion)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return nil, ErrConflict
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return s.Get(ctx, in.UID)
}

// SetFinalizersInput replaces the finalizer set, guarded by resource_version.
type SetFinalizersInput struct {
	UID             string
	ResourceVersion int64
	Finalizers      []string
}

// SetFinalizers performs a guarded finalizer patch.
func (s *Store) SetFinalizers(ctx context.Context, in SetFinalizersInput) (*model.Object, error) {
	ts := now().UTC()
	res, err := s.db.ExecContext(ctx, `
UPDATE resources SET finalizers = ?, resource_version = resource_version + 1,
    updated_at = ?
WHERE uid = ? AND resource_version = ?`,
		encodeJSON(in.Finalizers), ts.Format(time.RFC3339Nano),
		in.UID, in.ResourceVersion)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		// Distinguish missing from stale: check existence.
		if _, gerr := s.Get(ctx, in.UID); gerr != nil {
			return nil, gerr
		}
		return nil, ErrConflict
	}
	return s.Get(ctx, in.UID)
}

// Delete marks an object terminating. It is a no-op if already terminating.
// Physical removal happens via Purge after finalizers are gone.
func (s *Store) Delete(ctx context.Context, uid string) (*model.Object, error) {
	ts := now().UTC()
	res, err := s.db.ExecContext(ctx, `
UPDATE resources SET deletion_ts = ?, updated_at = ?
WHERE uid = ? AND deletion_ts IS NULL`,
		ts.Format(time.RFC3339Nano), ts.Format(time.RFC3339Nano), uid)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		o, gerr := s.Get(ctx, uid)
		if gerr != nil {
			return nil, gerr
		}
		if o.DeletionTS == nil {
			return nil, ErrConflict
		}
	}
	return s.Get(ctx, uid)
}

// Purge physically removes an object. It only succeeds when no finalizers
// remain (the controller must have removed its own after external cleanup).
func (s *Store) Purge(ctx context.Context, uid string) error {
	o, err := s.Get(ctx, uid)
	if err != nil {
		return err
	}
	if len(o.Finalizers) != 0 {
		return fmt.Errorf("%w: finalizers still present: %v", ErrRefused, o.Finalizers)
	}
	res, err := s.db.ExecContext(ctx, `DELETE FROM resources WHERE uid = ?`, uid)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

// StatusPatch is the controller-owned status write.
type StatusPatch struct {
	UID             string
	ResourceVersion int64 // optimistic guard
	ObservedGen     int64
	ExternalID      string
	State           string
	Conditions      []model.Condition
}

// UpdateStatus applies a guarded status write. It refuses (ErrRefused) an
// observedGeneration greater than the current spec generation, and silently
// refuses a regression by returning ErrConflict so the caller re-reads and
// retries rather than clobbering newer observations.
func (s *Store) UpdateStatus(ctx context.Context, p StatusPatch) (*model.Object, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	o, err := getForUpdate(ctx, tx, p.UID)
	if err != nil {
		return nil, err
	}
	if o.ResourceVer != p.ResourceVersion {
		return nil, ErrConflict
	}
	if p.ObservedGen > o.Generation {
		return nil, fmt.Errorf("%w: observedGeneration %d > generation %d",
			ErrRefused, p.ObservedGen, o.Generation)
	}
	if p.ObservedGen < o.Status.ObservedGeneration {
		// Stale status write: do not move observedGeneration backwards.
		return nil, fmt.Errorf("%w: observedGeneration %d < recorded %d",
			ErrConflict, p.ObservedGen, o.Status.ObservedGeneration)
	}
	status := model.Status{
		ObservedGeneration: p.ObservedGen,
		ExternalID:         p.ExternalID,
		State:              p.State,
		Conditions:         p.Conditions,
	}
	ts := now().UTC()
	res, err := tx.ExecContext(ctx, `
UPDATE resources SET status = ?, resource_version = resource_version + 1,
    updated_at = ?
WHERE uid = ? AND resource_version = ?`,
		encodeJSON(status), ts.Format(time.RFC3339Nano), p.UID, p.ResourceVersion)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return nil, ErrConflict
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return s.Get(ctx, p.UID)
}
