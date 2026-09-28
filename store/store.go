// Package store is the PostgreSQL-backed state storage. It is intentionally
// dumb about termination detection: it persists the kernel's facts, provides
// row-locked claiming and worker liveness, and exposes raw event streams. All
// DS decisions live in dsnet/kernel; the independent test oracle reads the
// same tables this package writes.
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"dsnet/proto"
)

//go:embed schema.sql
var schemaSQL string

// ErrNotFound is returned for missing runs/transfers.
var ErrNotFound = errors.New("not found")

// Store wraps the connection pool.
type Store struct {
	pool *pgxpool.Pool
}

// Open connects, verifies reachability and applies the schema.
func Open(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	s := &Store{pool: pool}
	if err := s.Migrate(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() { s.pool.Close() }
func (s *Store) Pool() *pgxpool.Pool { return s.pool }

// Migrate applies schema.sql. Idempotent.
func (s *Store) Migrate(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, schemaSQL)
	if err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	return nil
}

// Reset truncates every table. Used by tests and by `make reset`.
func (s *Store) Reset(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `TRUNCATE events, transfers, tasks, workers, runs RESTART IDENTITY CASCADE`)
	return err
}

// AppendBatch persists one decided fact batch inside a per-run transaction.
// Advisory lock key is derived from run_id so concurrent decisions over the
// same run serialise; different runs proceed in parallel. The callback
// receives the live tx for coupled writes (transfer/task materialisation).

// AppendBatch appends events and runs fn(tx) in the same transaction.
func (s *Store) AppendBatch(ctx context.Context, runID string, events []proto.Event,
	fn func(pgx.Tx) error) error {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	if _, err := conn.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", runID); err != nil {
		return err
	}
	tx, err := conn.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	for _, ev := range events {
		if err := insertEvent(ctx, tx, ev); err != nil {
			return err
		}
	}
	if fn != nil {
		if err := fn(tx); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

func insertEvent(ctx context.Context, tx pgx.Tx, ev proto.Event) error {
	payload := map[string]any{
		"kind": ev.Kind, "node_id": ev.NodeID, "from_node": ev.FromNode,
		"to_node": ev.ToNode, "transfer_id": ev.TransferID,
		"task_id": ev.TaskID, "parent_task": ev.ParentTask,
		"signal": string(ev.Signal), "partition": ev.Partition, "reason": ev.Reason,
	}
	_, err := tx.Exec(ctx, `
		INSERT INTO events(run_id, seq, kind, node_id, from_node, to_node,
			transfer_id, task_id, parent_task, signal, partition, reason, at, payload)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
		ev.RunID, ev.Seq, ev.Kind, ev.NodeID, ev.FromNode, ev.ToNode,
		ev.TransferID, ev.TaskID, ev.ParentTask, ev.Signal, ev.Partition,
		ev.Reason, ev.At, payload)
	return err
}

// RunRow is the persisted run metadata.
type RunRow struct {
	ID           string
	ClientRef    string
	Phase        proto.RunPhase
	BudgetMs     int64
	SubmittedAt  time.Time
	Deadline     *time.Time
	FailureClass proto.FailureClass
	FailureMsg   string
	LastEventSeq int64
}

// CreateRun inserts run metadata (pairs with the kernel start event batch).
func (s *Store) CreateRun(ctx context.Context, r RunRow) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO runs(run_id, client_ref, phase, budget_ms, submitted_at,
			deadline, failure_class, failure_msg, last_event_seq)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,0)
		ON CONFLICT (run_id) DO NOTHING`,
		r.ID, r.ClientRef, r.Phase, r.BudgetMs, r.SubmittedAt, r.Deadline,
		string(r.FailureClass), r.FailureMsg)
	return err
}

// GetRun loads run metadata.
func (s *Store) GetRun(ctx context.Context, runID string) (*RunRow, error) {
	row := s.pool.QueryRow(ctx, `
		SELECT run_id, client_ref, phase, budget_ms, submitted_at, deadline,
		       failure_class, failure_msg, last_event_seq
		FROM runs WHERE run_id=$1`, runID)
	r := &RunRow{}
	var phase, fc string
	var deadline *time.Time
	err := row.Scan(&r.ID, &r.ClientRef, &phase, &r.BudgetMs, &r.SubmittedAt,
		&deadline, &fc, &r.FailureMsg, &r.LastEventSeq)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("%w: run %s", ErrNotFound, runID)
	}
	if err != nil {
		return nil, err
	}
	r.Phase = proto.RunPhase(phase)
	r.FailureClass = proto.FailureClass(fc)
	r.Deadline = deadline
	return r, nil
}

// MarkRun updates terminal phase / failure / last seq after an event batch.
func (s *Store) MarkRun(ctx context.Context, tx pgx.Tx, runID string,
	phase proto.RunPhase, fc proto.FailureClass, msg string, lastSeq int64) error {
	_, err := tx.Exec(ctx, `
		UPDATE runs SET phase=$2, failure_class=$3, failure_msg=$4,
			last_event_seq=$5 WHERE run_id=$1`,
		runID, string(phase), string(fc), msg, lastSeq)
	return err
}

// Events returns the ordered event stream for a run.
func (s *Store) Events(ctx context.Context, runID string, fromSeq int64) ([]proto.Event, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT run_id, seq, kind, node_id, from_node, to_node, transfer_id, task_id,
		       parent_task, signal, partition, reason, at
		FROM events WHERE run_id=$1 AND seq > $2 ORDER BY seq`, runID, fromSeq)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEvents(rows)
}

// AllEvents returns every run's events in global insertion order (replay
// service / oracle cross-run checks).
func (s *Store) AllEvents(ctx context.Context) ([]proto.Event, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT run_id, seq, kind, node_id, from_node, to_node, transfer_id, task_id,
		       parent_task, signal, partition, reason, at
		FROM events ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEvents(rows)
}

func scanEvents(rows pgx.Rows) ([]proto.Event, error) {
	var out []proto.Event
	for rows.Next() {
		var ev proto.Event
		var kind, sig string
		if err := rows.Scan(&ev.RunID, &ev.Seq, &kind, &ev.NodeID, &ev.FromNode,
			&ev.ToNode, &ev.TransferID, &ev.TaskID, &ev.ParentTask, &sig,
			&ev.Partition, &ev.Reason, &ev.At); err != nil {
			return nil, err
		}
		ev.Kind = proto.EventKind(kind)
		ev.Signal = proto.SignalKind(sig)
		out = append(out, ev)
	}
	return out, rows.Err()
}
