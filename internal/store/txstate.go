package store

import (
	"context"
	"time"
)

// TxState is the coordinator's full read model of one group, read inside ONE
// transaction. Approve's atomic re-check builds its decision from this so the
// decision can never be based on rows a concurrent writer already changed.
type TxState struct {
	Policy           PolicyRow
	Selector         SelectorRow
	LatestObservedAt time.Time
	Instances        []InstanceRow
	Approved         []EvictionRow
}

// ReadTxState reads the full coordination state of a group using q, which may
// be a live *sql.Tx. Exported because the coordinator's approve closure must
// evaluate against rows locked by the surrounding transaction.
func ReadTxState(ctx context.Context, q DBTX, group string) (TxState, error) {
	var st TxState
	var err error
	if st.Policy, err = getPolicy(q, ctx, group); err != nil {
		return st, err
	}
	if st.Selector, err = currentSelector(q, ctx, group); err != nil {
		return st, err
	}
	var latestMs *int64
	if err := q.QueryRowContext(ctx,
		`SELECT MAX(observed_at) FROM observations WHERE group_name = ?`, group).Scan(&latestMs); err != nil {
		return st, err
	}
	if latestMs != nil {
		st.LatestObservedAt = time.UnixMilli(*latestMs)
	}
	rows, err := q.QueryContext(ctx, `
SELECT i.instance_id, i.group_name, i.labels, i.selector_version,
       COALESCE(i.last_observed_at, 0),
       COALESCE((
           SELECT orw.state
           FROM observation_rows orw
           JOIN observations o ON o.id = orw.observation_id
           WHERE orw.instance_id = i.instance_id
           ORDER BY o.observed_at DESC, o.id DESC
           LIMIT 1
       ), '') AS state
FROM instances i
WHERE i.group_name = ?`, group)
	if err != nil {
		return st, err
	}
	defer rows.Close()
	for rows.Next() {
		var in InstanceRow
		var lastObs int64
		if err := rows.Scan(&in.ID, &in.Group, &in.Labels, &in.SelVersion,
			&lastObs, &in.State); err != nil {
			return st, err
		}
		if lastObs > 0 {
			in.LastObservedAt = time.UnixMilli(lastObs)
		}
		st.Instances = append(st.Instances, in)
	}
	if err := rows.Err(); err != nil {
		return st, err
	}
	arows, err := q.QueryContext(ctx, `
SELECT id, group_name, instance_id, phase, outcome_reason, detail,
       selector_version, created_at, COALESCE(approved_at,0), COALESCE(expires_at,0),
       COALESCE(terminal_at,0), COALESCE(observation_id,0)
FROM evictions WHERE group_name = ? AND phase = 'approved'`, group)
	if err != nil {
		return st, err
	}
	defer arows.Close()
	st.Approved, err = scanEvictionRows(arows)
	return st, err
}

// ListGroups returns the names of all configured groups.
func (s *Store) ListGroups(ctx context.Context) ([]string, error) {
	var out []string
	err := s.WithTx(ctx, func(q DBTX) error {
		rows, err := q.QueryContext(ctx, `SELECT group_name FROM groups ORDER BY group_name`)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var g string
			if err := rows.Scan(&g); err != nil {
				return err
			}
			out = append(out, g)
		}
		return rows.Err()
	})
	return out, err
}
