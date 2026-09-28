package storage

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"lifecycle.local/v1/internal/model"
)

// AddOwnerRef inserts one ownership edge. Duplicate edges (same target
// resource, same owner uid) are rejected with ErrKindRefAlreadyExists.
func (s *sqliteStore) AddOwnerRef(ctx context.Context, q Queryer, resourceUID string, ref model.OwnerRef, pos int) error {
	_, err := q.ExecContext(ctx, `
        INSERT INTO resource_owner_refs(resource_uid, position, owner_uid,
            owner_namespace, owner_name, owner_kind, owner_api_version, block_deletion)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)`,
		resourceUID, pos, ref.UID, ref.Namespace, ref.Name, ref.Kind, ref.APIVersion, boolToInt(ref.BlockOwnerDeletion))
	if err != nil {
		if isUniqueConstraint(err) {
			return model.Errorf(model.ErrKindRefAlreadyExists,
				"ownerRef to %s (%s) already present on resource %s", ref.Name, ref.UID, resourceUID)
		}
		return model.Errorf(model.ErrKindStorage, "add owner ref: %v", err)
	}
	return nil
}

func (s *sqliteStore) RemoveOwnerRef(ctx context.Context, q Queryer, resourceUID, ownerUID string) error {
	res, err := q.ExecContext(ctx,
		`DELETE FROM resource_owner_refs WHERE resource_uid = ? AND owner_uid = ?`,
		resourceUID, ownerUID)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "remove owner ref: %v", err)
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return model.Errorf(model.ErrKindNotFound,
			"ownerRef %s not found on resource %s", ownerUID, resourceUID)
	}
	return nil
}

func (s *sqliteStore) ListOwnerRefs(ctx context.Context, q Queryer, resourceUID string) ([]model.OwnerRef, error) {
	rows, err := q.QueryContext(ctx, `
        SELECT owner_uid, owner_namespace, owner_name, owner_kind, owner_api_version, block_deletion
          FROM resource_owner_refs WHERE resource_uid = ? ORDER BY position, owner_uid`,
		resourceUID)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list owner refs: %v", err)
	}
	defer rows.Close()
	var out []model.OwnerRef
	for rows.Next() {
		var ref model.OwnerRef
		var blocked int
		if err := rows.Scan(&ref.UID, &ref.Namespace, &ref.Name, &ref.Kind, &ref.APIVersion, &blocked); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan owner ref: %v", err)
		}
		ref.BlockOwnerDeletion = blocked != 0
		out = append(out, ref)
	}
	return out, rowsErr(rows)
}

func (s *sqliteStore) ListAllOwnerRefs(ctx context.Context, q Queryer) (map[string][]model.OwnerRef, error) {
	rows, err := q.QueryContext(ctx, `
        SELECT resource_uid, owner_uid, owner_namespace, owner_name, owner_kind, owner_api_version, block_deletion
          FROM resource_owner_refs ORDER BY resource_uid, position, owner_uid`)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list all owner refs: %v", err)
	}
	defer rows.Close()
	out := map[string][]model.OwnerRef{}
	for rows.Next() {
		var (
			ruid string
			ref  model.OwnerRef
			blk  int
		)
		if err := rows.Scan(&ruid, &ref.UID, &ref.Namespace, &ref.Name, &ref.Kind, &ref.APIVersion, &blk); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan all owner ref: %v", err)
		}
		ref.BlockOwnerDeletion = blk != 0
		out[ruid] = append(out[ruid], ref)
	}
	return out, rowsErr(rows)
}

func (s *sqliteStore) AddFinalizer(ctx context.Context, q Queryer, resourceUID, finalizer string, pos int) error {
	_, err := q.ExecContext(ctx,
		`INSERT INTO resource_finalizers(resource_uid, finalizer, position) VALUES(?, ?, ?)`,
		resourceUID, finalizer, pos)
	if err != nil {
		if isUniqueConstraint(err) {
			return model.Errorf(model.ErrKindAlreadyExists,
				"finalizer %q already present on %s", finalizer, resourceUID)
		}
		return model.Errorf(model.ErrKindStorage, "add finalizer: %v", err)
	}
	return nil
}

func (s *sqliteStore) RemoveFinalizer(ctx context.Context, q Queryer, resourceUID, finalizer string) error {
	res, err := q.ExecContext(ctx,
		`DELETE FROM resource_finalizers WHERE resource_uid = ? AND finalizer = ?`,
		resourceUID, finalizer)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "remove finalizer: %v", err)
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return model.Errorf(model.ErrKindNotFound,
			"finalizer %q not present on %s", finalizer, resourceUID)
	}
	return nil
}

func (s *sqliteStore) ListFinalizers(ctx context.Context, q Queryer, resourceUID string) ([]string, error) {
	rows, err := q.QueryContext(ctx,
		`SELECT finalizer FROM resource_finalizers WHERE resource_uid = ? ORDER BY position, finalizer`,
		resourceUID)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list finalizers: %v", err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var f string
		if err := rows.Scan(&f); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan finalizer: %v", err)
		}
		out = append(out, f)
	}
	return out, rowsErr(rows)
}

func (s *sqliteStore) SetCondition(ctx context.Context, q Queryer, resourceUID string, c model.Condition) error {
	_, err := q.ExecContext(ctx, `
        INSERT INTO resource_conditions(resource_uid, cond_type, status, reason,
            message, observed_gen, last_trans)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(resource_uid, cond_type) DO UPDATE SET
            status = excluded.status, reason = excluded.reason,
            message = excluded.message, observed_gen = excluded.observed_gen,
            last_trans = excluded.last_trans`,
		resourceUID, c.Type, c.Status, c.Reason, c.Message, c.ObservedGeneration,
		c.LastTransition.UTC().Format(time.RFC3339Nano))
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "set condition: %v", err)
	}
	return nil
}

func (s *sqliteStore) DeleteCondition(ctx context.Context, q Queryer, resourceUID, condType string) error {
	_, err := q.ExecContext(ctx,
		`DELETE FROM resource_conditions WHERE resource_uid = ? AND cond_type = ?`,
		resourceUID, condType)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "delete condition: %v", err)
	}
	return nil
}

func (s *sqliteStore) DeleteConditions(ctx context.Context, q Queryer, resourceUID string) error {
	_, err := q.ExecContext(ctx,
		`DELETE FROM resource_conditions WHERE resource_uid = ?`, resourceUID)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "delete conditions: %v", err)
	}
	return nil
}

func (s *sqliteStore) ListConditions(ctx context.Context, q Queryer, resourceUID string) ([]model.Condition, error) {
	rows, err := q.QueryContext(ctx, `
        SELECT cond_type, status, reason, message, observed_gen, last_trans
          FROM resource_conditions WHERE resource_uid = ? ORDER BY cond_type`,
		resourceUID)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list conditions: %v", err)
	}
	defer rows.Close()
	var out []model.Condition
	for rows.Next() {
		var c model.Condition
		var ts string
		if err := rows.Scan(&c.Type, &c.Status, &c.Reason, &c.Message, &c.ObservedGeneration, &ts); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan condition: %v", err)
		}
		if t, err := time.Parse(time.RFC3339Nano, ts); err == nil {
			c.LastTransition = t
		}
		out = append(out, c)
	}
	return out, rowsErr(rows)
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func rowsErr(rows *sql.Rows) error {
	if err := rows.Err(); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		return model.Errorf(model.ErrKindStorage, "rows: %v", err)
	}
	return nil
}
