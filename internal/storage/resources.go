package storage

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"

	"lifecycle.local/v1/internal/model"
)

const resourceColumns = `uid, namespace, name, kind, api_version, spec,
    deletion_ts, deletion_policy, generation, resource_version, created_ts, updated_ts`

func scanResource(row interface {
	Scan(dest ...any) error
}) (*model.Resource, error) {
	var (
		r             model.Resource
		spec          []byte
		deletionTS    sql.NullString
		deletionPolicy sql.NullString
		createdTS     string
		updatedTS     string
	)
	if err := row.Scan(
		&r.UID, &r.Namespace, &r.Name, &r.Kind, &r.APIVersion, &spec,
		&deletionTS, &deletionPolicy, &r.Generation, &r.ResourceVersion,
		&createdTS, &updatedTS,
	); err != nil {
		return nil, err
	}
	r.Spec = spec
	r.DeletionPolicy = deletionPolicy.String
	if deletionTS.Valid && deletionTS.String != "" {
		if t, err := time.Parse(time.RFC3339Nano, deletionTS.String); err == nil {
			r.DeletionTimestamp = &t
		}
	}
	t, err := time.Parse(time.RFC3339Nano, createdTS)
	if err == nil {
		r.CreationTimestamp = t
	}
	return &r, nil
}

func (s *sqliteStore) loadAggregate(ctx context.Context, q Queryer, r *model.Resource) error {
	var err error
	if r.OwnerRefs, err = s.ListOwnerRefs(ctx, q, r.UID); err != nil {
		return err
	}
	if r.Finalizers, err = s.ListFinalizers(ctx, q, r.UID); err != nil {
		return err
	}
	if r.Conditions, err = s.ListConditions(ctx, q, r.UID); err != nil {
		return err
	}
	return nil
}

func (s *sqliteStore) GetResource(ctx context.Context, q Queryer, namespace, name string) (*model.Resource, error) {
	row := q.QueryRowContext(ctx,
		`SELECT `+resourceColumns+` FROM resources WHERE namespace = ? AND name = ?`,
		namespace, name)
	r, err := scanResource(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, model.Errorf(model.ErrKindNotFound, "resource %s/%s not found", namespace, name)
	}
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "get resource: %v", err)
	}
	if err := s.loadAggregate(ctx, q, r); err != nil {
		return nil, err
	}
	return r, nil
}

func (s *sqliteStore) GetResourceByUID(ctx context.Context, q Queryer, uid string) (*model.Resource, error) {
	row := q.QueryRowContext(ctx,
		`SELECT `+resourceColumns+` FROM resources WHERE uid = ?`, uid)
	r, err := scanResource(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, model.Errorf(model.ErrKindNotFound, "resource uid %s not found", uid)
	}
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "get resource by uid: %v", err)
	}
	if err := s.loadAggregate(ctx, q, r); err != nil {
		return nil, err
	}
	return r, nil
}

func (s *sqliteStore) ListResources(ctx context.Context, q Queryer) ([]*model.Resource, error) {
	rows, err := q.QueryContext(ctx, `SELECT `+resourceColumns+` FROM resources ORDER BY namespace, name`)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list resources: %v", err)
	}
	defer rows.Close()
	var out []*model.Resource
	for rows.Next() {
		r, err := scanResource(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	if err := rows.Err(); err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list resources rows: %v", err)
	}
	for _, r := range out {
		if err := s.loadAggregate(ctx, q, r); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func (s *sqliteStore) InsertResource(ctx context.Context, q Queryer, r *model.Resource) error {
	created := r.CreationTimestamp.UTC().Format(time.RFC3339Nano)
	updated := created
	_, err := q.ExecContext(ctx, `
        INSERT INTO resources(uid, namespace, name, kind, api_version, spec,
            deletion_ts, deletion_policy, generation, resource_version, created_ts, updated_ts)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		r.UID, r.Namespace, r.Name, r.Kind, r.APIVersion, r.Spec,
		nilIfZero(r.DeletionTimestamp), r.DeletionPolicy,
		r.Generation, r.ResourceVersion, created, updated)
	if err != nil {
		if isUniqueConstraint(err) {
			return model.Errorf(model.ErrKindAlreadyExists,
				"resource %s/%s already exists (or uid %s collision)", r.QualifiedName(), r.UID)
		}
		return model.Errorf(model.ErrKindStorage, "insert resource: %v", err)
	}
	for i := range r.OwnerRefs {
		if err := s.AddOwnerRef(ctx, q, r.UID, r.OwnerRefs[i], i); err != nil {
			return err
		}
	}
	for i, f := range r.Finalizers {
		if err := s.AddFinalizer(ctx, q, r.UID, f, i); err != nil {
			return err
		}
	}
	return nil
}

func (s *sqliteStore) UpdateResource(ctx context.Context, q Queryer, r *model.Resource) error {
	rv := r.ResourceVersion
	r.ResourceVersion = rv + 1
	now := r.CreationTimestamp // placeholder; callers manage timestamps
	res, err := q.ExecContext(ctx, `
        UPDATE resources
           SET spec = ?, deletion_ts = ?, deletion_policy = ?,
               generation = ?, resource_version = ?, updated_ts = ?
         WHERE uid = ? AND resource_version = ?`,
		r.Spec, nilIfZero(r.DeletionTimestamp), r.DeletionPolicy,
		r.Generation, r.ResourceVersion, now.UTC().Format(time.RFC3339Nano),
		r.UID, rv)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "update resource: %v", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "update resource rows: %v", err)
	}
	if n == 0 {
		// Restore caller-visible rv for a clean error.
		r.ResourceVersion = rv
		return model.Errorf(model.ErrKindConflict,
			"resource %s was modified concurrently (rv %d)", r.QualifiedName(), rv)
	}
	return nil
}

func (s *sqliteStore) DeleteResource(ctx context.Context, q Queryer, uid string) error {
	// Child rows cascade via ON DELETE CASCADE (finalizers, refs,
	// conditions); gc_events/tombstones are retained deliberately.
	res, err := q.ExecContext(ctx, `DELETE FROM resources WHERE uid = ?`, uid)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "delete resource: %v", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "delete resource rows: %v", err)
	}
	if n == 0 {
		return model.Errorf(model.ErrKindNotFound, "resource uid %s not found", uid)
	}
	return nil
}

func nilIfZero(t *time.Time) any {
	if t == nil {
		return nil
	}
	return t.UTC().Format(time.RFC3339Nano)
}

func isUniqueConstraint(err error) bool {
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint failed")
}
