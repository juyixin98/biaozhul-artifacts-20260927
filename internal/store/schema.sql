-- opp291 coordinator schema.
--
-- The event log (group_events) is the single source of truth. All coordinator
-- state is rebuilt by folding events; the projections tables (members,
-- partitions) are maintained in the same append transaction purely as a
-- convenient read model and can be regenerated from the log at any time.

CREATE TABLE IF NOT EXISTS groups_meta (
    group_id           TEXT PRIMARY KEY,
    partition_count    INTEGER NOT NULL CHECK (partition_count > 0),
    session_timeout_ms BIGINT  NOT NULL,
    generation         BIGINT  NOT NULL DEFAULT 0,
    phase              TEXT    NOT NULL DEFAULT 'STABLE',
    version            BIGINT  NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS group_events (
    seq         BIGSERIAL PRIMARY KEY,
    group_id    TEXT NOT NULL REFERENCES groups_meta(group_id) ON DELETE CASCADE,
    version     BIGINT NOT NULL,
    type        TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    request_id  TEXT NOT NULL,
    generation  BIGINT NOT NULL DEFAULT 0,
    force       BOOLEAN NOT NULL DEFAULT FALSE,
    payload     JSONB NOT NULL,
    UNIQUE (group_id, version)
);
CREATE INDEX IF NOT EXISTS idx_group_events_group_version
    ON group_events (group_id, version);

CREATE TABLE IF NOT EXISTS members_proj (
    group_id   TEXT NOT NULL REFERENCES groups_meta(group_id) ON DELETE CASCADE,
    member_id  TEXT NOT NULL,
    generation BIGINT NOT NULL,
    joined_at  TIMESTAMPTZ NOT NULL,
    last_seen  TIMESTAMPTZ NOT NULL,
    active     BOOLEAN NOT NULL,
    PRIMARY KEY (group_id, member_id)
);

CREATE TABLE IF NOT EXISTS partitions_proj (
    group_id        TEXT NOT NULL REFERENCES groups_meta(group_id) ON DELETE CASCADE,
    partition       INTEGER NOT NULL,
    phase           TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '',
    generation      BIGINT NOT NULL DEFAULT 0,
    prev_owner      TEXT NOT NULL DEFAULT '',
    prev_generation BIGINT NOT NULL DEFAULT 0,
    pending_owner   TEXT NOT NULL DEFAULT '',
    offset_value    BIGINT NOT NULL DEFAULT 0,
    offset_owner    TEXT NOT NULL DEFAULT '',
    offset_generation BIGINT NOT NULL DEFAULT 0,
    uncertain       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (group_id, partition)
);

-- Correlated diagnostic log. Every request writes one or more trace rows under
-- a shared request_id so an operator can follow: validate -> plan -> append ->
-- fold, including version, processing location and failure category.
CREATE TABLE IF NOT EXISTS request_traces (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    member_id    TEXT NOT NULL DEFAULT '',
    generation   BIGINT NOT NULL DEFAULT 0,
    step         TEXT NOT NULL,
    version      BIGINT NOT NULL DEFAULT 0,
    location     TEXT NOT NULL DEFAULT '',
    ok           BOOLEAN NOT NULL,
    fail_code    TEXT NOT NULL DEFAULT '',
    fail_detail  TEXT NOT NULL DEFAULT '',
    uncertain    BOOLEAN NOT NULL DEFAULT FALSE,
    extra        JSONB
);
CREATE INDEX IF NOT EXISTS idx_request_traces_request
    ON request_traces (request_id, id);
