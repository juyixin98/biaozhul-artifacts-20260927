-- SQLite schema for the local lifecycle controller.
-- Identity note: resources.uid is the incarnation identity. (namespace,
-- name) is UNIQUE only among live rows: after a physical delete a
-- same-named resource can be recreated with a NEW uid, and owner refs
-- pinned to the old uid must not resolve against it.

CREATE TABLE IF NOT EXISTS resources (
    uid                TEXT PRIMARY KEY,
    namespace          TEXT NOT NULL,
    name               TEXT NOT NULL,
    kind               TEXT NOT NULL,
    api_version        TEXT NOT NULL,
    spec               BLOB NOT NULL DEFAULT (''),
    deletion_ts        TEXT,
    deletion_policy    TEXT NOT NULL DEFAULT '',
    generation         INTEGER NOT NULL DEFAULT 1,
    resource_version   INTEGER NOT NULL DEFAULT 1,
    created_ts         TEXT NOT NULL,
    updated_ts         TEXT NOT NULL,
    UNIQUE (namespace, name)
);

CREATE TABLE IF NOT EXISTS resource_finalizers (
    resource_uid TEXT NOT NULL REFERENCES resources(uid) ON DELETE CASCADE,
    finalizer    TEXT NOT NULL,
    position     INTEGER NOT NULL,
    PRIMARY KEY (resource_uid, finalizer)
);

CREATE TABLE IF NOT EXISTS resource_owner_refs (
    resource_uid       TEXT NOT NULL REFERENCES resources(uid) ON DELETE CASCADE,
    position           INTEGER NOT NULL,
    owner_uid          TEXT NOT NULL,
    owner_namespace    TEXT NOT NULL,
    owner_name         TEXT NOT NULL,
    owner_kind         TEXT NOT NULL,
    owner_api_version  TEXT NOT NULL,
    block_deletion     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (resource_uid, owner_uid)
);

CREATE TABLE IF NOT EXISTS resource_conditions (
    resource_uid TEXT NOT NULL REFERENCES resources(uid) ON DELETE CASCADE,
    cond_type    TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    observed_gen INTEGER NOT NULL DEFAULT 0,
    last_trans   TEXT NOT NULL,
    PRIMARY KEY (resource_uid, cond_type)
);

CREATE TABLE IF NOT EXISTS gc_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    tick        INTEGER NOT NULL,
    step        INTEGER NOT NULL,
    type        TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    uid         TEXT NOT NULL DEFAULT '',
    other_uid   TEXT NOT NULL DEFAULT '',
    other_name  TEXT NOT NULL DEFAULT '',
    policy      TEXT NOT NULL DEFAULT '',
    finalizer   TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);

-- Tombses of owners that were physically deleted, retaining the
-- propagation policy of their deletion for cascade evaluation and for
-- audit after the owner row is gone.
CREATE TABLE IF NOT EXISTS deleted_owners (
    owner_uid  TEXT PRIMARY KEY,
    namespace  TEXT NOT NULL,
    name       TEXT NOT NULL,
    policy     TEXT NOT NULL,
    deleted_ts TEXT NOT NULL,
    tick       INTEGER NOT NULL
);

-- Diagnosed ownership cycles and the deterministic break decision.
CREATE TABLE IF NOT EXISTS cycle_marks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick        INTEGER NOT NULL,
    cycle_uids  TEXT NOT NULL,   -- JSON array, ordered
    broken_uid  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    noted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resources_ns_name ON resources(namespace, name);
CREATE INDEX IF NOT EXISTS idx_refs_owner ON resource_owner_refs(owner_uid);
CREATE INDEX IF NOT EXISTS idx_refs_resource ON resource_owner_refs(resource_uid);
CREATE INDEX IF NOT EXISTS idx_resources_deleting ON resources(deletion_ts);
CREATE INDEX IF NOT EXISTS idx_gc_events_run_tick ON gc_events(run_id, tick, id);
