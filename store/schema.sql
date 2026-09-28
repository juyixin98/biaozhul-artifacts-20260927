-- Dijkstra-Scholten task network: relational state store.
-- The events table is the append-only source of truth. runs/transfers are
-- materialised conveniences, rebuilt and cross-checked by the replay service.

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    client_ref    TEXT NOT NULL DEFAULT '',
    phase         TEXT NOT NULL DEFAULT 'running',
    budget_ms     BIGINT NOT NULL DEFAULT 0,
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deadline      TIMESTAMPTZ,
    failure_class TEXT NOT NULL DEFAULT '',
    failure_msg   TEXT NOT NULL DEFAULT '',
    last_event_seq BIGINT NOT NULL DEFAULT 0
);

-- Append-only event log. (run_id, seq) is gap-free per run because append
-- happens under a per-run advisory lock.
CREATE TABLE IF NOT EXISTS events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    seq          BIGINT NOT NULL,
    kind         TEXT NOT NULL,
    node_id      TEXT NOT NULL DEFAULT '',
    from_node    TEXT NOT NULL DEFAULT '',
    to_node      TEXT NOT NULL DEFAULT '',
    transfer_id  TEXT NOT NULL DEFAULT '',
    task_id      TEXT NOT NULL DEFAULT '',
    parent_task  TEXT NOT NULL DEFAULT '',
    signal       TEXT NOT NULL DEFAULT '',
    partition    TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    at           TIMESTAMPTZ NOT NULL,
    payload      JSONB NOT NULL,
    PRIMARY KEY (run_id, seq)
);
COMMENT ON COLUMN events.id IS 'global insertion order for cross-run log scans';

-- Current edge state. The UNIQUE constraint on transfer_id is the idempotency
-- fence: a retried transfer/signal with the same identity can never create a
-- second row or decrement a counter twice.
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    from_node    TEXT NOT NULL,
    to_node      TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    partition    TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'open',  -- open|settled|unacknowledged
    receipted    BOOLEAN NOT NULL DEFAULT FALSE,
    disengaged   BOOLEAN NOT NULL DEFAULT FALSE,
    claimed_by   TEXT NOT NULL DEFAULT '',
    started      BOOLEAN NOT NULL DEFAULT FALSE,
    outcome      TEXT NOT NULL DEFAULT '',      -- ''|succeeded|failed
    opened_seq   BIGINT NOT NULL,
    settled_seq  BIGINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transfers_claim
    ON transfers(run_id, partition, state, opened_seq)
    WHERE state = 'open' AND claimed_by = '' AND started = FALSE;

-- Task outcomes (one row per task identity). Completion is idempotent per
-- transfer; the transfer_id changes on a re-delivered task, but the task
-- identity moves with the work.
CREATE TABLE IF NOT EXISTS tasks (
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    task_id      TEXT NOT NULL,
    transfer_id  TEXT NOT NULL,
    parent_task  TEXT NOT NULL DEFAULT '',
    op           TEXT NOT NULL,
    partition    TEXT NOT NULL,
    spec         JSONB NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL DEFAULT 'queued', -- queued|running|succeeded|failed
    reason       TEXT NOT NULL DEFAULT '',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, task_id)
);

-- Worker liveness. A node identity for DS is (worker_id, partition): each
-- subscription is one place where queued tasks can become active.
CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT NOT NULL,
    partition    TEXT NOT NULL,
    run_id       TEXT NOT NULL DEFAULT '',
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (worker_id, partition)
);
CREATE INDEX IF NOT EXISTS idx_workers_live ON workers(partition, last_seen);
