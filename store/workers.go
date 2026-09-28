package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
)

// Heartbeat upserts a worker subscription to one partition.
func (s *Store) Heartbeat(ctx context.Context, worker, partition, runID string) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO workers(worker_id, partition, run_id, last_seen)
		VALUES ($1,$2,$3, now())
		ON CONFLICT (worker_id, partition)
		DO UPDATE SET last_seen=now(), run_id=EXCLUDED.run_id`,
		worker, partition, runID)
	return err
}

// LiveWorker returns one worker that recently heartbeated for a partition, or ""
// if none exists. TTL defines "recent".
func (s *Store) LiveWorker(ctx context.Context, partition string, ttl time.Duration) (string, error) {
	var w string
	err := s.pool.QueryRow(ctx, `
		SELECT worker_id FROM workers
		WHERE partition=$1 AND last_seen > now() - $2::interval
		ORDER BY last_seen DESC LIMIT 1`,
		partition, ttl.String()).Scan(&w)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return w, nil
}

// LivePartitions returns partitions with at least one live worker.
func (s *Store) LivePartitions(ctx context.Context, ttl time.Duration) (map[string]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT ON (partition) partition, worker_id
		FROM workers WHERE last_seen > now() - $1::interval
		ORDER BY partition, last_seen DESC`, ttl.String())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var p, w string
		if err := rows.Scan(&p, &w); err != nil {
			return nil, err
		}
		out[p] = w
	}
	return out, rows.Err()
}
