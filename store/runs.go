package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
)

// RunningRun is a run the budget sweeper must inspect.
type RunningRun struct {
	ID       string
	Deadline *time.Time
}

// ListRunning returns non-terminal runs, optionally only those with a deadline.
func (s *Store) ListRunning(ctx context.Context) ([]RunningRun, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT run_id, deadline FROM runs
		WHERE phase='running' AND deadline IS NOT NULL
		ORDER BY submitted_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RunningRun
	for rows.Next() {
		var r RunningRun
		if err := rows.Scan(&r.ID, &r.Deadline); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// TaskCounts is the SQL-side workload projection.
type TaskCounts struct {
	Queued, Running, Succeeded, Failed int64
}

func (s *Store) CountTasks(ctx context.Context, runID string) (TaskCounts, error) {
	var c TaskCounts
	err := s.pool.QueryRow(ctx, `
		SELECT
		  count(*) FILTER (WHERE state='queued'),
		  count(*) FILTER (WHERE state='running'),
		  count(*) FILTER (WHERE state='succeeded'),
		  count(*) FILTER (WHERE state='failed')
		FROM tasks WHERE run_id=$1`, runID).Scan(&c.Queued, &c.Running, &c.Succeeded, &c.Failed)
	return c, err
}

// RunExists is a cheap existence check.
func (s *Store) RunExists(ctx context.Context, runID string) (bool, error) {
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT 1 FROM runs WHERE run_id=$1`, runID).Scan(&n)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
