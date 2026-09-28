package store

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"dsnet/kernel"
	"dsnet/proto"
)

// persistDecision 在加锁事务内把一次归约的结果落库：
// 任务行 upsert、确定性产出、事件日志追加、终态作业更新。
func persistDecision(
	ctx context.Context,
	tx pgx.Tx,
	st *kernel.State,
	d *kernel.Decision,
	requestID string,
) error {
	snap := st.Snapshot()
	views := make(map[proto.TaskID]kernel.TaskView, len(snap.Tasks))
	for _, v := range snap.Tasks {
		views[v.Spec.TaskID] = v
	}

	// 1) upsert 被本命令触及的任务（新身份 INSERT，其余 UPDATE）。
	for _, id := range st.Touched() {
		v, ok := views[id]
		if !ok {
			return proto.Fail(proto.FailInternal, "被触及任务 %s 不在快照中", id)
		}
		payloadRaw, err := json.Marshal(v.Spec.Payload)
		if err != nil {
			return fmt.Errorf("marshal payload: %w", err)
		}
		var leaseUntil *time.Time
		if !v.LeaseUntil.IsZero() {
			t := v.LeaseUntil
			leaseUntil = &t
		}
		_, err = tx.Exec(ctx, `
			INSERT INTO tasks (
				job_id, task_id, parent_id, kind, depth, payload, status,
				engaged, passive, settled, deficit,
				lease_id, worker_id, lease_until, created_at, updated_at
			) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$15)
			ON CONFLICT (job_id, task_id) DO UPDATE SET
				parent_id=EXCLUDED.parent_id,
				kind=EXCLUDED.kind,
				depth=EXCLUDED.depth,
				payload=EXCLUDED.payload,
				status=EXCLUDED.status,
				engaged=EXCLUDED.engaged,
				passive=EXCLUDED.passive,
				settled=EXCLUDED.settled,
				deficit=EXCLUDED.deficit,
				lease_id=EXCLUDED.lease_id,
				worker_id=EXCLUDED.worker_id,
				lease_until=EXCLUDED.lease_until,
				updated_at=EXCLUDED.updated_at
		`,
			st.JobID, v.Spec.TaskID, v.Spec.ParentID, string(v.Spec.Kind), v.Spec.Depth,
			payloadRaw, string(v.Status), v.Engaged, v.Passive, v.Settled, v.Deficit,
			string(v.LeaseID), string(v.WorkerID), leaseUntil, time.Now())
		if err != nil {
			return fmt.Errorf("upsert task %s: %w", id, err)
		}
	}

	// 2) 确定性产出（叶子摘要等）。
	for _, r := range d.Reported {
		if _, err := tx.Exec(ctx, `
			INSERT INTO task_outputs (job_id, task_id, kind, output_digest, spawned, reported_at)
			VALUES ($1,$2,$3,$4,$5,$6)
			ON CONFLICT (job_id, task_id) DO UPDATE SET
				kind=EXCLUDED.kind,
				output_digest=EXCLUDED.output_digest,
				spawned=EXCLUDED.spawned,
				reported_at=EXCLUDED.reported_at
		`, st.JobID, r.TaskID, string(r.Kind), r.OutputDigest, r.Spawned, time.Now()); err != nil {
			return fmt.Errorf("upsert output %s: %w", r.TaskID, err)
		}
	}

	// 3) 追加事件日志（seq 由数据库按作业分配）。
	for _, ev := range d.Events {
		detailRaw, err := json.Marshal(ev.Detail)
		if err != nil {
			return fmt.Errorf("marshal detail: %w", err)
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO events (
				job_id, type, task_id, parent_id, edge_id, lease_id,
				worker_id, request_id, detail, occurred_at
			) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
		`,
			ev.JobID, string(ev.Type), ev.TaskID, ev.ParentID, ev.EdgeID, ev.LeaseID,
			ev.WorkerID, ev.RequestID, detailRaw, time.Now()); err != nil {
			return fmt.Errorf("insert event %s: %w", ev.Type, err)
		}
	}

	// 4) 终态作业。
	if d.Terminal != nil {
		reason, reqID := terminalMeta(d)
		if _, err := tx.Exec(ctx, `
			UPDATE jobs SET phase=$2, terminal_reason=$3,
				terminal_request_id=$4, updated_at=$5
			WHERE id=$1
		`, st.JobID, string(*d.Terminal), reason, reqID, time.Now()); err != nil {
			return fmt.Errorf("update job terminal: %w", err)
		}
	}
	return nil
}

func terminalMeta(d *kernel.Decision) (reason, requestID string) {
	for _, ev := range d.Events {
		if ev.Type == proto.EvJobComplete || ev.Type == proto.EvJobFailed {
			if r, ok := ev.Detail["reason"].(string); ok {
				reason = r
			}
			return reason, ev.RequestID
		}
	}
	return "terminal", requestID
}

// CreateJob 持久化一个新作业：jobs 行、根任务行、job.created 事件在一个事务内。
func (s *Store) CreateJob(
	ctx context.Context,
	jobID proto.JobID,
	rootTaskID proto.TaskID,
	planMap map[string]any,
	budgetDeadline *time.Time,
	requestID string,
) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", proto.AdvisoryLockKey(jobID)); err != nil {
		return fmt.Errorf("lock: %w", err)
	}
	planRaw, err := json.Marshal(planMap)
	if err != nil {
		return fmt.Errorf("marshal plan: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO jobs (id, phase, root_task_id, plan, budget_deadline, created_at, updated_at)
		VALUES ($1,'running',$2,$3,$4,$5,$5)
	`, jobID, rootTaskID, planRaw, budgetDeadline, time.Now()); err != nil {
		return fmt.Errorf("insert job: %w", err)
	}
	payloadRaw, _ := json.Marshal(planMap)
	if _, err := tx.Exec(ctx, `
		INSERT INTO tasks (
			job_id, task_id, parent_id, kind, depth, payload, status,
			engaged, passive, settled, deficit, created_at, updated_at
		) VALUES ($1,$2,'','root',0,$3,'ready',true,false,false,0,$4,$4)
	`, jobID, rootTaskID, payloadRaw, time.Now()); err != nil {
		return fmt.Errorf("insert root task: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO events (
			job_id, type, task_id, request_id, detail, occurred_at
		) VALUES ($1,'job.created',$2,$3,$4,$5)
	`, jobID, rootTaskID, requestID, planRaw, time.Now()); err != nil {
		return fmt.Errorf("insert job.created: %w", err)
	}
	return tx.Commit(ctx)
}
