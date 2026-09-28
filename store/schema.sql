-- dsnet 状态存储 schema（PostgreSQL 13+，本地开发库 dsnet）。
-- 设计：物化状态（jobs/tasks/task_outputs）+ 仅追加事件日志（events）。
-- 每个作业的变更在单个事务内执行，事务先取分区咨询锁，
-- 使同一作业的 DS 计数变更是全序的；不同分区可并行。

CREATE TABLE IF NOT EXISTS jobs (
    id                 TEXT PRIMARY KEY,
    phase              TEXT NOT NULL,                 -- running|complete|failed
    root_task_id       TEXT NOT NULL,
    plan               JSONB NOT NULL,                -- compute.Plan
    budget_deadline    TIMESTAMPTZ,                   -- 为空表示无预算
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminal_reason    TEXT NOT NULL DEFAULT '',
    terminal_request_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    job_id        TEXT NOT NULL REFERENCES jobs(id),
    task_id       TEXT NOT NULL,
    parent_id     TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL,                     -- ready|claimed|reported|timed_out
    engaged       BOOLEAN NOT NULL,
    passive       BOOLEAN NOT NULL,
    settled       BOOLEAN NOT NULL,
    deficit       BIGINT NOT NULL,
    lease_id      TEXT NOT NULL DEFAULT '',
    worker_id     TEXT NOT NULL DEFAULT '',
    lease_until   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, task_id)
);

-- 认领查询：按作业挑选最早的 ready 任务。
CREATE INDEX IF NOT EXISTS tasks_ready_idx
    ON tasks (job_id, created_at, task_id)
    WHERE status = 'ready';

CREATE INDEX IF NOT EXISTS tasks_claimed_idx
    ON tasks (lease_until)
    WHERE status = 'claimed';

-- 任务的确定性产出（叶子为哈希链摘要），供核对与重放比对。
CREATE TABLE IF NOT EXISTS task_outputs (
    job_id       TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    output_digest TEXT NOT NULL DEFAULT '',
    spawned      INTEGER NOT NULL DEFAULT 0,
    reported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, task_id)
);

-- 仅追加事件日志。seq 按作业单调递增，是回放接口的输入。
CREATE TABLE IF NOT EXISTS events (
    seq          BIGSERIAL,
    job_id       TEXT NOT NULL,
    type         TEXT NOT NULL,
    task_id      TEXT NOT NULL DEFAULT '',
    parent_id    TEXT NOT NULL DEFAULT '',
    edge_id      TEXT NOT NULL DEFAULT '',
    lease_id     TEXT NOT NULL DEFAULT '',
    worker_id    TEXT NOT NULL DEFAULT '',
    request_id   TEXT NOT NULL DEFAULT '',
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, seq)
);

-- 全局事件浏览/跨作业预算扫描。
CREATE INDEX IF NOT EXISTS events_type_idx ON events (type);
CREATE INDEX IF NOT EXISTS jobs_budget_idx
    ON jobs (budget_deadline)
    WHERE phase = 'running';
