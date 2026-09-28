package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"dsnet/proto"
)

// TransferRow is the materialised causal edge.
type TransferRow struct {
	ID         string
	RunID      string
	FromNode   string
	ToNode     string
	TaskID     string
	Partition  string
	State      proto.TransferState
	Receipted  bool
	Disengaged bool
	ClaimedBy  string
	Started    bool
	Outcome    string
	OpenedSeq  int64
	SettledSeq int64
	ParentTask string
	Op         proto.TaskOp
	Spec       proto.TaskSpec
}

// InsertTransfer materialises one opened edge inside tx.
func InsertTransfer(ctx context.Context, tx pgx.Tx, t TransferRow) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO transfers(transfer_id, run_id, from_node, to_node, task_id,
			partition, state, receipted, disengaged, opened_seq)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
		ON CONFLICT (transfer_id) DO NOTHING`,
		t.ID, t.RunID, t.FromNode, t.ToNode, t.TaskID, t.Partition,
		string(t.State), t.Receipted, t.Disengaged, t.OpenedSeq)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO tasks(run_id, task_id, transfer_id, parent_task, op, partition, spec)
		VALUES ($1,$2,$3,$4,$5,$6,$7)
		ON CONFLICT (run_id, task_id) DO NOTHING`,
		t.RunID, t.TaskID, t.ID, t.ParentTask, string(t.Op), t.Partition,
		mustJSON(t.Spec))
	return err
}

// GetTransfer loads one edge.
func (s *Store) GetTransfer(ctx context.Context, id string) (*TransferRow, error) {
	row := s.pool.QueryRow(ctx, `
		SELECT `+transferCols+`
		FROM transfers WHERE transfer_id=$1`, id)
	t, err := scanTransfer(row)
	if err != nil {
		return nil, err
	}
	if tk, ok := s.taskMeta(ctx, t.RunID, t.TaskID); ok {
		t.ParentTask, t.Op = tk.parent, tk.op
	}
	return t, nil
}

type taskMeta struct {
	parent string
	op     proto.TaskOp
}

func (s *Store) taskMeta(ctx context.Context, runID, taskID string) (taskMeta, bool) {
	var m taskMeta
	var op string
	err := s.pool.QueryRow(ctx,
		`SELECT COALESCE(parent_task,''), op FROM tasks WHERE run_id=$1 AND task_id=$2`,
		runID, taskID).Scan(&m.parent, &op)
	if err != nil {
		return taskMeta{}, false
	}
	m.op = proto.TaskOp(op)
	return m, true
}

// Claim atomically takes one queued edge for (worker, partition). SKIP LOCKED
// means two workers polling the same partition never get the same task.
func (s *Store) Claim(ctx context.Context, runID, worker, partition string) (*TransferRow, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if runID != "" {
		if _, err := tx.Exec(ctx,
			"SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", runID); err != nil {
			return nil, err
		}
	}
	row := tx.QueryRow(ctx, `
		UPDATE transfers SET claimed_by=$1, updated_at=now()
		WHERE transfer_id = (
			SELECT transfer_id FROM transfers
			WHERE ($3='' OR run_id=$3)
			  AND partition=$2 AND state='open'
			  AND claimed_by='' AND started=FALSE
			ORDER BY opened_seq
			FOR UPDATE SKIP LOCKED
			LIMIT 1)
		RETURNING transfer_id, run_id, from_node, to_node, task_id, partition,
		          state, receipted, disengaged, claimed_by, started, outcome,
		          opened_seq, settled_seq`,
		worker, partition, runID)
	t, err := scanTransfer(row)
	if errors.Is(err, pgx.ErrNoRows) {
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx, `
		UPDATE tasks SET state='running', updated_at=now()
		WHERE run_id=$1 AND task_id=$2`, t.RunID, t.TaskID); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return t, nil
}

// MarkStarted records task start.
func (s *Store) MarkStarted(ctx context.Context, tx pgx.Tx, transferID string) error {
	_, err := tx.Exec(ctx,
		`UPDATE transfers SET started=TRUE, updated_at=now() WHERE transfer_id=$1`,
		transferID)
	return err
}

// MarkOutcome records terminal task outcome and optionally the edge state.
// The tasks row is always moved to succeeded/failed; the transfers edge
// settlement is applied separately via SettleEdge/MarkUnacked so that task
// completion and DS edge settlement stay independently auditable.
func (s *Store) MarkOutcome(ctx context.Context, tx pgx.Tx, transferID, outcome, settleState string, settledSeq int64) error {
	if _, err := tx.Exec(ctx, `
		UPDATE tasks SET state=CASE WHEN $2='succeeded' THEN 'succeeded' ELSE 'failed' END,
			updated_at=now()
		WHERE transfer_id=$1`, transferID, outcome); err != nil {
		return err
	}
	if settleState == "" {
		_, err := tx.Exec(ctx, `
			UPDATE transfers SET outcome=$2, updated_at=now() WHERE transfer_id=$1`,
			transferID, outcome)
		return err
	}
	_, err := tx.Exec(ctx, `
		UPDATE transfers SET outcome=$2, state=$3, settled_seq=$4, updated_at=now()
		WHERE transfer_id=$1`, transferID, outcome, settleState, settledSeq)
	return err
}

// CountOpen returns state counts straight from SQL (used by the oracle).
type EdgeCounts struct {
	Open, Settled, Unacked int64
	ClaimedOpen            int64
}

func (s *Store) CountEdges(ctx context.Context, runID string) (EdgeCounts, error) {
	var c EdgeCounts
	row := s.pool.QueryRow(ctx, `
		SELECT
		  count(*) FILTER (WHERE state='open'),
		  count(*) FILTER (WHERE state='settled'),
		  count(*) FILTER (WHERE state='unacknowledged'),
		  count(*) FILTER (WHERE state='open' AND claimed_by<>'')
		FROM transfers WHERE run_id=$1`, runID)
	err := row.Scan(&c.Open, &c.Settled, &c.Unacked, &c.ClaimedOpen)
	return c, err
}

// ListOpenTransfers returns still-open edges (budget sweeper).
func (s *Store) ListOpenTransfers(ctx context.Context, runID string) ([]TransferRow, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT `+transferCols+`
		FROM transfers WHERE run_id=$1 AND state='open' ORDER BY opened_seq`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TransferRow
	for rows.Next() {
		t, err := scanTransfer(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *t)
	}
	return out, rows.Err()
}

// MarkUnacked marks an edge unacknowledged (budget path) inside tx.
func (s *Store) MarkUnacked(ctx context.Context, tx pgx.Tx, transferID string, seq int64) error {
	_, err := tx.Exec(ctx, `
		UPDATE transfers SET state='unacknowledged', settled_seq=$2, updated_at=now()
		WHERE transfer_id=$1 AND state='open'`, transferID, seq)
	return err
}

// SettleEdge marks a normally-disengaged edge settled inside tx.
func SettleEdge(ctx context.Context, tx pgx.Tx, transferID string, seq int64) error {
	_, err := tx.Exec(ctx, `
		UPDATE transfers SET state='settled', settled_seq=$2, updated_at=now()
		WHERE transfer_id=$1 AND state='open'`, transferID, seq)
	return err
}

// ExpireOpenTasks flips queued/running tasks under an expired budget.
func (s *Store) ExpireOpenTasks(ctx context.Context, tx pgx.Tx, runID string) error {
	_, err := tx.Exec(ctx, `
		UPDATE tasks SET state='failed', reason='budget:unacknowledged', updated_at=now()
		WHERE run_id=$1 AND state IN ('queued','running')`, runID)
	return err
}

func scanTransfer(row pgx.Row) (*TransferRow, error) {
	var t TransferRow
	var state string
	if err := row.Scan(&t.ID, &t.RunID, &t.FromNode, &t.ToNode, &t.TaskID,
		&t.Partition, &state, &t.Receipted, &t.Disengaged, &t.ClaimedBy,
		&t.Started, &t.Outcome, &t.OpenedSeq, &t.SettledSeq); err != nil {
		return nil, err
	}
	t.State = proto.TransferState(state)
	return &t, nil
}

// transferCols is the canonical 14-column transfer projection.
const transferCols = `transfer_id, run_id, from_node, to_node, task_id,
		partition, state, receipted, disengaged, claimed_by, started, outcome,
		opened_seq, settled_seq`

// DeadlineSoon reports whether a deadline already passed.
func DeadlineSoon(deadline *time.Time, now time.Time) bool {
	return deadline != nil && !deadline.After(now)
}
