-- Package store schema. Applied idempotently at open time.
-- All mutable state lives here; the scheduler itself is stateless.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id        TEXT PRIMARY KEY,
    region    TEXT NOT NULL DEFAULT '',
    zone      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,
    capacity  TEXT NOT NULL,           -- JSON model.Resources
    labels    TEXT NOT NULL DEFAULT '{}',
    taints    TEXT NOT NULL DEFAULT '[]',
    version   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS instances (
    id           TEXT PRIMARY KEY,
    state        TEXT NOT NULL,        -- pending|bound|evicted|failed
    node_id      TEXT NOT NULL DEFAULT '',
    request      TEXT NOT NULL,        -- JSON model.Resources
    zone         TEXT NOT NULL DEFAULT '',
    selector     TEXT NOT NULL DEFAULT '{}',
    tolerations  TEXT NOT NULL DEFAULT '[]',
    groups_json  TEXT NOT NULL DEFAULT '{}',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_code    TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL         -- RFC3339 UTC
);

CREATE INDEX IF NOT EXISTS idx_instances_state ON instances(state);

-- Cluster-wide group policy is a single versioned document.
CREATE TABLE IF NOT EXISTS policy_doc (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    doc     TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,          -- plan|replace|reconcile
    status     TEXT NOT NULL,          -- feasible|conflict|error
    request    TEXT NOT NULL,
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
