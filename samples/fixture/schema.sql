-- Local synthetic read-only fixture used ONLY for schema/identifier review.
-- The reviewer never executes submitted SQL against this database; it opens
-- the resulting file immutable/read-only and reads the catalog plus asks the
-- planner for EXPLAIN QUERY PLAN.

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE VIEW active_users AS
SELECT id, name, email, created_at FROM users WHERE status = 'active';
