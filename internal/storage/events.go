package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"

	"lifecycle.local/v1/internal/model"
)

func (s *sqliteStore) AppendEvent(ctx context.Context, q Queryer, e *model.GCEvent) error {
	occ := e.OccurredAt
	if occ.IsZero() {
		occ = time.Now().UTC()
	}
	res, err := q.ExecContext(ctx, `
        INSERT INTO gc_events(run_id, tick, step, type, namespace, name, uid,
            other_uid, other_name, policy, finalizer, reason, message, occurred_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		e.RunID, e.Tick, e.Step, e.Type, e.Namespace, e.Name, e.UID,
		e.OtherUID, e.OtherName, e.Policy, e.Finalizer, e.Reason, e.Message,
		occ.UTC().Format(time.RFC3339Nano))
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "append event: %v", err)
	}
	id, err := res.LastInsertId()
	if err == nil {
		e.ID = id
	}
	return nil
}

func scanEvent(row interface {
	Scan(dest ...any) error
}) (model.GCEvent, error) {
	var (
		e      model.GCEvent
		occ    string
		policy sql.NullString
	)
	err := row.Scan(&e.ID, &e.RunID, &e.Tick, &e.Step, &e.Type, &e.Namespace, &e.Name,
		&e.UID, &e.OtherUID, &e.OtherName, &e.Policy, &e.Finalizer, &e.Reason,
		&e.Message, &occ)
	if err != nil {
		return e, err
	}
	if t, perr := time.Parse(time.RFC3339Nano, occ); perr == nil {
		e.OccurredAt = t
	}
	_ = policy
	return e, nil
}

const eventColumns = `id, run_id, tick, step, type, namespace, name, uid,
    other_uid, other_name, policy, finalizer, reason, message, occurred_at`

func (s *sqliteStore) ListEvents(ctx context.Context, runID string, limit int) ([]model.GCEvent, error) {
	if limit <= 0 {
		limit = 500
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT `+eventColumns+` FROM gc_events WHERE run_id = ? ORDER BY id LIMIT ?`,
		runID, limit)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list events: %v", err)
	}
	defer rows.Close()
	return collectEvents(rows)
}

func (s *sqliteStore) ListEventsForTick(ctx context.Context, runID string, tick int64) ([]model.GCEvent, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT `+eventColumns+` FROM gc_events WHERE run_id = ? AND tick = ? ORDER BY id`,
		runID, tick)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list events for tick: %v", err)
	}
	defer rows.Close()
	return collectEvents(rows)
}

func (s *sqliteStore) LastEventID(ctx context.Context, runID string) (int64, error) {
	var id sql.NullInt64
	err := s.db.QueryRowContext(ctx,
		`SELECT MAX(id) FROM gc_events WHERE run_id = ?`, runID).Scan(&id)
	if err != nil {
		return 0, model.Errorf(model.ErrKindStorage, "last event id: %v", err)
	}
	return id.Int64, nil
}

func collectEvents(rows *sql.Rows) ([]model.GCEvent, error) {
	var out []model.GCEvent
	for rows.Next() {
		e, err := scanEvent(rows)
		if err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan event: %v", err)
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "event rows: %v", err)
	}
	return out, nil
}

func (s *sqliteStore) InsertDeletedOwner(ctx context.Context, q Queryer, d model.DeletedOwner) error {
	_, err := q.ExecContext(ctx, `
        INSERT INTO deleted_owners(owner_uid, namespace, name, policy, deleted_ts, tick)
        VALUES(?, ?, ?, ?, ?, ?)`,
		d.UID, d.Namespace, d.Name, d.Policy,
		d.DeletedAt.UTC().Format(time.RFC3339Nano), d.Tick)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "insert deleted owner: %v", err)
	}
	return nil
}

func (s *sqliteStore) GetDeletedOwner(ctx context.Context, q Queryer, uid string) (*model.DeletedOwner, error) {
	var (
		d   model.DeletedOwner
		ts  string
	)
	err := q.QueryRowContext(ctx, `
        SELECT owner_uid, namespace, name, policy, deleted_ts, tick
          FROM deleted_owners WHERE owner_uid = ?`, uid).
		Scan(&d.UID, &d.Namespace, &d.Name, &d.Policy, &ts, &d.Tick)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "get deleted owner: %v", err)
	}
	if t, perr := time.Parse(time.RFC3339Nano, ts); perr == nil {
		d.DeletedAt = t
	}
	return &d, nil
}

func (s *sqliteStore) ListDeletedOwners(ctx context.Context, q Queryer) ([]model.DeletedOwner, error) {
	rows, err := q.QueryContext(ctx,
		`SELECT owner_uid, namespace, name, policy, deleted_ts, tick FROM deleted_owners ORDER BY id`)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list deleted owners: %v", err)
	}
	defer rows.Close()
	var out []model.DeletedOwner
	for rows.Next() {
		var d model.DeletedOwner
		var ts string
		if err := rows.Scan(&d.UID, &d.Namespace, &d.Name, &d.Policy, &ts, &d.Tick); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan deleted owner: %v", err)
		}
		if t, perr := time.Parse(time.RFC3339Nano, ts); perr == nil {
			d.DeletedAt = t
		}
		out = append(out, d)
	}
	return out, rowsErr(rows)
}

func (s *sqliteStore) InsertCycleMark(ctx context.Context, q Queryer, m *model.CycleMark) error {
	raw, err := json.Marshal(m.Cycle)
	if err != nil {
		return model.Errorf(model.ErrKindInternal, "marshal cycle: %v", err)
	}
	res, err := q.ExecContext(ctx, `
        INSERT INTO cycle_marks(tick, cycle_uids, broken_uid, reason, noted_at)
        VALUES(?, ?, ?, ?, ?)`,
		m.Tick, string(raw), m.BrokenUID, m.Reason,
		m.NotedAt.UTC().Format(time.RFC3339Nano))
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "insert cycle mark: %v", err)
	}
	id, err := res.LastInsertId()
	if err == nil {
		m.ID = id
	}
	return nil
}

func (s *sqliteStore) ListCycleMarks(ctx context.Context, q Queryer) ([]model.CycleMark, error) {
	rows, err := q.QueryContext(ctx,
		`SELECT id, tick, cycle_uids, broken_uid, reason, noted_at FROM cycle_marks ORDER BY id`)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "list cycle marks: %v", err)
	}
	defer rows.Close()
	var out []model.CycleMark
	for rows.Next() {
		var (
			m       model.CycleMark
			raw     string
			notedAt string
		)
		if err := rows.Scan(&m.ID, &m.Tick, &raw, &m.BrokenUID, &m.Reason, &notedAt); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "scan cycle mark: %v", err)
		}
		if err := json.Unmarshal([]byte(raw), &m.Cycle); err != nil {
			return nil, model.Errorf(model.ErrKindStorage, "unmarshal cycle mark: %v", err)
		}
		if t, perr := time.Parse(time.RFC3339Nano, notedAt); perr == nil {
			m.NotedAt = t
		}
		out = append(out, m)
	}
	return out, rowsErr(rows)
}
