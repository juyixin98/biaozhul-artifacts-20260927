-- local-txpool SQLite schema
-- 所有金额以 TEXT 存储十进制 wei（避免 JSON 大整数精度问题），比较时 CAST。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    address    TEXT PRIMARY KEY,           -- 小写 0x 地址
    balance    TEXT NOT NULL DEFAULT '0',  -- wei，十进制字符串
    nonce      INTEGER NOT NULL DEFAULT 0, -- 链上已执行 nonce
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_hash       TEXT PRIMARY KEY,        -- 0x 小写
    raw           BLOB NOT NULL,           -- 原始 RLP 编码字节（离线回放可重新解码验签）
    sender        TEXT NOT NULL,
    to_addr       TEXT,
    nonce         INTEGER NOT NULL,
    gas_price     TEXT NOT NULL,
    gas_limit     INTEGER NOT NULL,
    value         TEXT NOT NULL,
    data          BLOB NOT NULL DEFAULT x'',
    chain_id      INTEGER NOT NULL,
    received_at   INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    status        TEXT NOT NULL,           -- pending|queued|included|mined|expired|replaced|evicted
    reason        TEXT NOT NULL,           -- 当前状态的机器可读理由码（见 core/models.py）
    reason_detail TEXT NOT NULL DEFAULT '',
    replaced_by   TEXT,                    -- 当 status=replaced：替换者 tx_hash
    block_number  INTEGER,                 -- included/mined 时所在区块号
    position      INTEGER,                 -- 在区块中的位置
    updated_at    INTEGER NOT NULL
);

-- 关键索引约束：同一发送者同一 nonce，在 pending/queued/included 三类有效状态中至多一条。
-- mined 是已确认历史，不占唯一槽位；过期/替换/淘汰也释放槽位。
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_sender_nonce
    ON transactions(sender, nonce)
    WHERE status IN ('pending', 'queued', 'included');

CREATE INDEX IF NOT EXISTS ix_tx_status      ON transactions(status);
CREATE INDEX IF NOT EXISTS ix_tx_sender      ON transactions(sender, status, nonce);
CREATE INDEX IF NOT EXISTS ix_tx_expires     ON transactions(expires_at)
    WHERE status IN ('pending', 'queued', 'included');
CREATE INDEX IF NOT EXISTS ix_tx_gasprice    ON transactions(status, CAST(gas_price AS INTEGER));

CREATE TABLE IF NOT EXISTS blocks (
    number       INTEGER PRIMARY KEY,
    hash         TEXT NOT NULL,
    parent_hash  TEXT NOT NULL,
    gas_limit    INTEGER NOT NULL,
    gas_used     INTEGER NOT NULL,
    coinbase     TEXT NOT NULL,
    status       TEXT NOT NULL,             -- proposed|confirmed
    proposed_at  INTEGER NOT NULL,
    confirmed_at INTEGER
);

CREATE TABLE IF NOT EXISTS block_txs (
    block_number INTEGER NOT NULL REFERENCES blocks(number) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    tx_hash      TEXT NOT NULL,
    PRIMARY KEY (block_number, position)
);

-- 确认区块前对受影响账户拍下的快照；回滚逐块逆向恢复。
CREATE TABLE IF NOT EXISTS account_snapshots (
    block_number   INTEGER NOT NULL,
    address        TEXT NOT NULL,
    balance_before TEXT NOT NULL,
    nonce_before   INTEGER NOT NULL,
    PRIMARY KEY (block_number, address)
);

CREATE TABLE IF NOT EXISTS journals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    request_id   TEXT NOT NULL DEFAULT '',
    tx_hash      TEXT,
    sender       TEXT,
    block_number INTEGER,
    action       TEXT NOT NULL,
    from_status  TEXT NOT NULL DEFAULT '',
    to_status    TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT ''   -- JSON
);

CREATE INDEX IF NOT EXISTS ix_journal_tx    ON journals(tx_hash);
CREATE INDEX IF NOT EXISTS ix_journal_req   ON journals(request_id);
CREATE INDEX IF NOT EXISTS ix_journal_block ON journals(block_number);
CREATE INDEX IF NOT EXISTS ix_journal_ts    ON journals(ts);
