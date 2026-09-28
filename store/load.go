package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"dsnet/kernel"
	"dsnet/proto"
)

// jobRow 是 jobs 表的一行。
type jobRow struct {
	id               string
	phase            string
	rootTaskID       string
	plan             []byte
	budgetDeadline   pgtype.Timestamptz
	terminalReason   string
	terminalRequest  string
}

// loadState 在当前事务内把作业的物化状态重建成 kernel.State。
//
// 计数（spawnedEdges/signals/...）不在作业行冗余存储，而是从 tasks 与
// events 行即时推导，使物化状态与事件日志必须一致才能构成合法状态。
func loadState(ctx context.Context, tx pgx.Tx, jobID proto.JobID, now func() time.Time) (*kernel.State, error) {
	jr, err := readJob(ctx, tx, jobID)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(jr.plan, &payload); err != nil {
		return nil, fmt.Errorf("plan 反序列化: %w", err)
	}

	rootSpec := proto.TaskSpec{
		JobID:  jobID,
		TaskID: proto.TaskID(jr.rootTaskID),
		Kind:   proto.KindRoot,
		Depth:  0,
		Payload: payload,
	}
	deadline := time.Time{}
	if jr.budgetDeadline.Valid {
		deadline = jr.budgetDeadline.Time
	}
	st := kernel.NewState(jobID, rootSpec, deadline, now())
	st.SetClock(now)

	rows, err := tx.Query(ctx, `
		SELECT task_id, parent_id, kind, depth, payload, status,
		       engaged, passive, settled, deficit,
		       lease_id, worker_id, lease_until, created_at, updated_at
		FROM tasks WHERE job_id = $1 ORDER BY created_at, task_id`, jobID)
	if err != nil {
		return nil, fmt.Errorf("load tasks: %w", err)
	}
	defer rows.Close()

	var loaded int
	for rows.Next() {
		var (
			id, parent, kind      string
			depth                 int
			payloadRaw            []byte
			status                string
			engaged, passive, set bool
			deficit               int64
			leaseID, workerID     string
			leaseUntil            pgtype.Timestamptz
			createdAt, updatedAt  time.Time
		)
		if err := rows.Scan(&id, &parent, &kind, &depth, &payloadRaw, &status,
			&engaged, &passive, &set, &deficit,
			&leaseID, &workerID, &leaseUntil, &createdAt, &updatedAt); err != nil {
			return nil, fmt.Errorf("scan task: %w", err)
		}
		var pl map[string]any
		if err := json.Unmarshal(payloadRaw, &pl); err != nil {
			return nil, fmt.Errorf("task payload: %w", err)
		}
		view := kernel.TaskView{
			Spec: proto.TaskSpec{
				JobID:    jobID,
				TaskID:   proto.TaskID(id),
				ParentID: proto.TaskID(parent),
				Kind:     proto.TaskKind(kind),
				Depth:    depth,
				Payload:  pl,
			},
			Status:  proto.TaskStatus(status),
			Engaged:  engaged,
			Passive:  passive,
			Settled:  set,
			Deficit:  deficit,
			LeaseID:  proto.LeaseID(leaseID),
			WorkerID: proto.WorkerID(workerID),
		}
		if leaseUntil.Valid {
			view.LeaseUntil = leaseUntil.Time
		}
		if id == jr.rootTaskID {
			// 根已由 NewState 放入；用行中的真实视图替换。
			st.ReplaceTask(view)
		} else {
			st.InsertTask(view)
		}
		loaded++
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// 从事件日志推导计数，再与物化行交叉核对。
	var spawned, signals, reports, dup, unack int64
	err = tx.QueryRow(ctx, `
		SELECT
			count(*) FILTER (WHERE type='task.spawned'),
			count(*) FILTER (WHERE type='task.signal'),
			count(*) FILTER (WHERE type='task.reported'),
			count(*) FILTER (WHERE type='task.duplicate_ignored'),
			count(*) FILTER (WHERE type='task.timed_out')
		FROM events WHERE job_id=$1`, jobID).
		Scan(&spawned, &signals, &reports, &dup, &unack)
	if err != nil {
		return nil, fmt.Errorf("load event counts: %w", err)
	}
	st.SetCounters(spawned, signals, reports, dup, unack)
	st.SetPhase(proto.JobPhase(jr.phase))
	return st, nil
}

func readJob(ctx context.Context, tx pgx.Tx, jobID proto.JobID) (*jobRow, error) {
	var jr jobRow
	err := tx.QueryRow(ctx, `
		SELECT id, phase, root_task_id, plan, budget_deadline,
		       terminal_reason, terminal_request_id
		FROM jobs WHERE id=$1`, jobID).
		Scan(&jr.id, &jr.phase, &jr.rootTaskID, &jr.plan,
			&jr.budgetDeadline, &jr.terminalReason, &jr.terminalRequest)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, proto.Fail(proto.FailNotFound, "作业不存在: %s", jobID)
	}
	if err != nil {
		return nil, fmt.Errorf("load job: %w", err)
	}
	return &jr, nil
}
