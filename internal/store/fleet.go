package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"replicactl/internal/model"
)

// Fleet returns the currently active synthetic instances, ordered by
// allocation sequence.
func (s *Store) Fleet(ctx context.Context) (model.Fleet, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, created_at FROM instances
		 WHERE active=1 ORDER BY seq ASC`)
	if err != nil {
		return model.Fleet{}, err
	}
	defer rows.Close()
	f := model.Fleet{UpdatedAt: time.Time{}}
	for rows.Next() {
		var id, ts string
		if err := rows.Scan(&id, &ts); err != nil {
			return model.Fleet{}, err
		}
		f.Instances = append(f.Instances, id)
		if t, err := time.Parse(time.RFC3339Nano, ts); err == nil && t.After(f.UpdatedAt) {
			f.UpdatedAt = t
		}
	}
	return f, rows.Err()
}

// SeedFleet creates the initial fleet of n instances once. Existing active
// instances are left untouched, so a restart is a no-op.
func (s *Store) SeedFleet(ctx context.Context, n int32, now time.Time) error {
	return s.withTx(ctx, func(tx *sql.Tx) error {
		var count int
		if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM instances WHERE active=1`).Scan(&count); err != nil {
			return err
		}
		if count > 0 || n <= 0 {
			return nil
		}
		_, err := s.allocate(ctx, tx, int(n), now)
		return err
	})
}

// allocate appends n new instances. New IDs are derived from a monotonically
// increasing global sequence, so an ID is never reused after its instance is
// removed.
func (s *Store) allocate(ctx context.Context, tx *sql.Tx, n int, now time.Time) ([]string, error) {
	var seq int64
	row := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(seq),0) FROM instances`)
	if err := row.Scan(&seq); err != nil {
		return nil, err
	}
	nowStr := now.UTC().Format(time.RFC3339Nano)
	added := make([]string, 0, n)
	for i := 0; i < n; i++ {
		seq++
		id := fmt.Sprintf("ins-%04d", seq)
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO instances(id,active,created_at,removed_at) VALUES(?,1,?,NULL)`,
			id, nowStr); err != nil {
			return nil, err
		}
		added = append(added, id)
	}
	return added, nil
}

// ApplyScale is the actuator on the local fixture. It grows by appending new
// stable IDs and shrinks by deactivating the highest-numbered active IDs
// first (deterministic). The whole change is transactional.
func (s *Store) ApplyScale(ctx context.Context, want int32, now time.Time) (newActive, added, removed []string, err error) {
	err = s.withTx(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `SELECT id FROM instances WHERE active=1 ORDER BY seq ASC`)
		if err != nil {
			return err
		}
		var active []string
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			active = append(active, id)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		cur := len(active)
		if want < 0 {
			return fmt.Errorf("actuator refusing negative replica count %d", want)
		}
		nowStr := now.UTC().Format(time.RFC3339Nano)
		switch {
		case int(want) > cur:
			add, err := s.allocate(ctx, tx, int(want)-cur, now)
			if err != nil {
				return err
			}
			added = add
		case int(want) < cur:
			drop := active[want:]
			for _, id := range drop {
				if _, err := tx.ExecContext(ctx,
					`UPDATE instances SET active=0, removed_at=? WHERE id=? AND active=1`, nowStr, id); err != nil {
					return err
				}
			}
			removed = drop
		}
		nRows, err := tx.QueryContext(ctx, `SELECT id FROM instances WHERE active=1 ORDER BY seq ASC`)
		if err != nil {
			return err
		}
		defer nRows.Close()
		for nRows.Next() {
			var id string
			if err := nRows.Scan(&id); err != nil {
				return err
			}
			newActive = append(newActive, id)
		}
		return nRows.Err()
	})
	return newActive, added, removed, err
}
