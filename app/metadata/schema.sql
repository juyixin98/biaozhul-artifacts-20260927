-- RTDA 元数据 DDL（SQLite）。所有清单仅追加；文件不可变，重写以“新增文件 + 旧文件 DROP”表示。
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tables (
    table_id       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    schema_json    TEXT NOT NULL,           -- 列定义（固定 schema，本版不支持演进）
    primary_key    TEXT NOT NULL,           -- JSON 数组
    config_json    TEXT NOT NULL,           -- 表级资源上限
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id    TEXT PRIMARY KEY,
    table_id       TEXT NOT NULL REFERENCES tables(table_id),
    seq            INTEGER NOT NULL,        -- 表内单调递增序列号
    parent_id      TEXT,                    -- 根快照为 NULL
    created_at     TEXT NOT NULL,
    summary_json   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_snapshots_table_seq
    ON snapshots(table_id, seq);

-- 不可变数据文件。文件一旦写出内容即固定，content_hash 为身份的一部分。
CREATE TABLE IF NOT EXISTS data_files (
    file_id        TEXT PRIMARY KEY,
    table_id       TEXT NOT NULL REFERENCES tables(table_id),
    path           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    row_count      INTEGER NOT NULL,
    added_seq      INTEGER NOT NULL
);

-- 清单事件（仅追加）：ADD / DROP。快照 s 下文件存活 = 存在 seq<=s 的 ADD 且无 seq<=s 的 DROP。
CREATE TABLE IF NOT EXISTS manifest_entries (
    table_id       TEXT NOT NULL,
    file_id        TEXT NOT NULL REFERENCES data_files(file_id),
    seq            INTEGER NOT NULL,
    snapshot_id    TEXT NOT NULL,
    change         TEXT NOT NULL CHECK (change IN ('ADD', 'DROP')),
    reason         TEXT NOT NULL,           -- COMMIT / REWRITE
    PRIMARY KEY (file_id, seq, change)
);

-- 删除文件（位置删除 / 等值删除）。position: target_file_id 指向被删数据文件；
-- equality: key_columns JSON，按 seq 对所有更早数据文件生效。
CREATE TABLE IF NOT EXISTS delete_files (
    delete_file_id TEXT PRIMARY KEY,
    table_id       TEXT NOT NULL REFERENCES tables(table_id),
    path           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('POSITION', 'EQUALITY')),
    target_file_id TEXT REFERENCES data_files(file_id),
    key_columns    TEXT,                    -- JSON 数组，POSITION 为 NULL
    row_count      INTEGER NOT NULL,
    seq            INTEGER NOT NULL,
    snapshot_id    TEXT NOT NULL
);

-- 提交领域事件（逐版本事件参考的数据来源）。
CREATE TABLE IF NOT EXISTS event_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    ts             TEXT NOT NULL,
    table_id       TEXT NOT NULL,
    snapshot_id    TEXT,
    seq            INTEGER,
    event_type     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);

-- 每次接口调用一条：请求摘要、分阶段中间状态、判定理由、错误分类。
CREATE TABLE IF NOT EXISTS request_log (
    run_id         TEXT PRIMARY KEY,
    ts             TEXT NOT NULL,
    kind           TEXT NOT NULL,           -- CREATE / COMMIT / READ / EXPLAIN / VALIDATE
    table_id       TEXT,
    status         TEXT NOT NULL,          -- OK / ERROR
    request_json   TEXT NOT NULL,
    phases_json    TEXT NOT NULL,          -- JSON 数组：阶段名 + 中间状态 + 判断理由
    error_json     TEXT,
    duration_ms    REAL
);
