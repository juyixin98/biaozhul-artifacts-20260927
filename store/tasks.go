package store

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"

	"dsnet/proto"
)

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		return []byte("{}")
	}
	return b
}

// ClaimCandidate returns the oldest unclaimed open edge for a partition
// WITHOUT taking it. Used in the decide step; the materialise step re-selects
// with FOR UPDATE SKIP LOCKED and only wins if the identity still matches.
func (s *Store) ClaimCandidate(ctx context.Context, runID, partition string) (string, error) {
	var id string
	err := s.pool.QueryRow(ctx, `
		SELECT transfer_id FROM transfers
		WHERE ($1='' OR run_id=$1) AND partition=$2
		  AND state='open' AND claimed_by='' AND started=FALSE
		ORDER BY opened_seq LIMIT 1`, runID, partition).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return id, nil
}

// TakeClaim atomically claims transferID for worker inside tx if it is still
// free. Returns ok=false when another worker won the race.
func TakeClaim(ctx context.Context, tx pgx.Tx, transferID, worker string) (bool, error) {
	tag, err := tx.Exec(ctx, `
		UPDATE transfers SET claimed_by=$2, updated_at=now()
		WHERE transfer_id=$1 AND state='open'
		  AND claimed_by='' AND started=FALSE`, transferID, worker)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// GetTaskSpec loads the stored spec needed to interpret spawn ops on complete.
func (s *Store) GetTaskSpec(ctx context.Context, runID, taskID string) (proto.TaskSpec, error) {
	var spec proto.TaskSpec
	var raw []byte
	err := s.pool.QueryRow(ctx,
		`SELECT spec FROM tasks WHERE run_id=$1 AND task_id=$2`, runID, taskID).
		Scan(&raw)
	if err != nil {
		return spec, err
	}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &spec)
	}
	return spec, nil
}
