// Package store 提供基于 SQLite 的持久化：
//   - routes 表保存“当前最新状态”，进程重启后可直接装载；
//   - events 表是只追加的变更事件日志（upsert/delete/replace_all），
//     回放接口按 seq 顺序重放，可与当前状态做一致性对照。
//
// 所有写操作在单个事务内完成，且事件与快照同事务写入：调用方要么
// 同时看到事件和对应状态，要么都看不到。
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	_ "modernc.org/sqlite"

	"rib/internal/netmodel"
)

// EventType 是变更事件类型。
type EventType string

const (
	EventUpsert     EventType = "upsert"
	EventDelete     EventType = "delete"
	EventReplaceAll EventType = "replace_all"
)

// Event 是事件日志中的一条记录。Payload 依类型不同：
//   - upsert: 单个路由 JSON；
//   - delete: {"prefix": ..., "id": ...}；
//   - replace_all: {"v4":[路由...], "v6":[路由...]}。
type Event struct {
	Seq       int64           `json:"seq"`
	Type      EventType       `json:"type"`
	Version   int64           `json:"version"`
	RequestID string          `json:"request_id"`
	Payload   json.RawMessage `json:"payload"`
}

// ReplacePayload 是 replace_all 事件的载荷，独立于 RIB 包定义，
// 避免 store 依赖上层。
type ReplacePayload struct {
	V4 []netmodel.Route `json:"v4"`
	V6 []netmodel.Route `json:"v6"`
}

// DeletePayload 是 delete 事件的载荷。
type DeletePayload struct {
	Prefix string `json:"prefix"`
	ID     string `json:"id"`
}

// Store 封装数据库句柄。
type Store struct {
	db *sql.DB
}

// Open 打开（必要时创建）数据库文件并初始化表结构。
// ":memory:" 可用于测试。busy_timeout 降低并发写时的锁竞争报错。
func Open(ctx context.Context, dsn string) (*Store, error) {
	if dsn == "" {
		return nil, errors.New("empty sqlite dsn")
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// SQLite 单文件写串行；单连接避免 SQLITE_BUSY 并保证事件顺序。
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, `PRAGMA busy_timeout = 5000`); err != nil {
		db.Close()
		return nil, err
	}
	s := &Store{db: db}
	if err := s.init(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) init(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS routes (
			family   INTEGER NOT NULL,
			prefix   TEXT    NOT NULL,
			route_id TEXT    NOT NULL,
			version  INTEGER NOT NULL,
			data     TEXT    NOT NULL,
			PRIMARY KEY (family, prefix, route_id)
		)`,
		`CREATE TABLE IF NOT EXISTS events (
			seq        INTEGER PRIMARY KEY AUTOINCREMENT,
			kind       TEXT    NOT NULL,
			version    INTEGER NOT NULL,
			request_id TEXT    NOT NULL DEFAULT '',
			payload    TEXT    NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS meta (
			key   TEXT PRIMARY KEY,
			value TEXT NOT NULL
		)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("init schema: %w", err)
		}
	}
	return nil
}

// Close 关闭数据库。
func (s *Store) Close() error { return s.db.Close() }

// LoadRoutes 读取当前全部已持久化路由，供进程启动时重建 RIB。
func (s *Store) LoadRoutes(ctx context.Context) ([]netmodel.Route, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT data FROM routes ORDER BY family, prefix, route_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []netmodel.Route
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var rt netmodel.Route
		if err := json.Unmarshal([]byte(raw), &rt); err != nil {
			return nil, fmt.Errorf("corrupt route row: %w", err)
		}
		out = append(out, rt)
	}
	return out, rows.Err()
}

// CommitUpsert 在单事务内更新快照并追加事件。返回事件序号。
func (s *Store) CommitUpsert(ctx context.Context, rt netmodel.Route, version int64, requestID string) (int64, error) {
	raw, err := json.Marshal(rt)
	if err != nil {
		return 0, err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	if err := upsertRow(ctx, tx, int(rt.Prefix.Family()), rt.Prefix.String(), rt.ID, version, string(raw)); err != nil {
		return 0, err
	}
	seq, err := insertEvent(ctx, tx, EventUpsert, version, requestID, raw)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return seq, nil
}

// CommitDelete 记录删除：从快照移除行并追加 delete 事件。
// 目标不存在时返回 sql.ErrNoRows（不加事件），由上层映射失败类别。
func (s *Store) CommitDelete(ctx context.Context, fam int, prefix, id string, version int64, requestID string) (int64, error) {
	payload, _ := json.Marshal(DeletePayload{Prefix: prefix, ID: id})
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	res, err := tx.ExecContext(ctx,
		`DELETE FROM routes WHERE family=? AND prefix=? AND route_id=?`, fam, prefix, id)
	if err != nil {
		return 0, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return 0, sql.ErrNoRows
	}
	seq, err := insertEvent(ctx, tx, EventDelete, version, requestID, payload)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return seq, nil
}

// CommitReplace 在单事务内清空快照、写入新全量路由、追加一条 replace_all 事件。
func (s *Store) CommitReplace(ctx context.Context, req ReplacePayload, version int64, requestID string) (int64, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return 0, err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `DELETE FROM routes`); err != nil {
		return 0, err
	}
	for _, group := range [][]netmodel.Route{req.V4, req.V6} {
		for _, rt := range group {
			raw, err := json.Marshal(rt)
			if err != nil {
				return 0, err
			}
			if err := upsertRow(ctx, tx, int(rt.Prefix.Family()), rt.Prefix.String(), rt.ID, version, string(raw)); err != nil {
				return 0, err
			}
		}
	}
	seq, err := insertEvent(ctx, tx, EventReplaceAll, version, requestID, payload)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return seq, nil
}

func upsertRow(ctx context.Context, tx *sql.Tx, fam int, prefix, id string, version int64, raw string) error {
	_, err := tx.ExecContext(ctx,
		`INSERT INTO routes(family,prefix,route_id,version,data)
		 VALUES(?,?,?,?,?)
		 ON CONFLICT(family,prefix,route_id) DO UPDATE SET version=excluded.version, data=excluded.data`,
		fam, prefix, id, version, raw)
	return err
}

func insertEvent(ctx context.Context, tx *sql.Tx, kind EventType, version int64, requestID string, payload []byte) (int64, error) {
	res, err := tx.ExecContext(ctx,
		`INSERT INTO events(kind,version,request_id,payload) VALUES(?,?,?,?)`,
		string(kind), version, requestID, string(payload))
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// EventsSince 按 seq 顺序读取事件（回放接口使用）。sinceSeq 传 0 表示从头。
// 一次读入；夹具场景事件量小。
func (s *Store) EventsSince(ctx context.Context, sinceSeq int64, limit int) ([]Event, error) {
	q := `SELECT seq,kind,version,request_id,payload FROM events WHERE seq>? ORDER BY seq`
	args := []any{sinceSeq}
	if limit > 0 {
		q += ` LIMIT ?`
		args = append(args, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var e Event
		var kind string
		var raw string
		if err := rows.Scan(&e.Seq, &kind, &e.Version, &e.RequestID, &raw); err != nil {
			return nil, err
		}
		e.Type = EventType(kind)
		e.Payload = json.RawMessage(raw)
		out = append(out, e)
	}
	return out, rows.Err()
}

// MaxEventSeq 返回事件日志的最大序号（空日志为 0）。
func (s *Store) MaxEventSeq(ctx context.Context) (int64, error) {
	var maxSeq sql.NullInt64
	if err := s.db.QueryRowContext(ctx, `SELECT MAX(seq) FROM events`).Scan(&maxSeq); err != nil {
		return 0, err
	}
	return maxSeq.Int64, nil
}

// SetMeta / GetMeta 保存少量引导信息（如表版本）。
func (s *Store) SetMeta(ctx context.Context, key, value string) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO meta(key,value) VALUES(?,?)
		 ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value)
	return err
}

func (s *Store) GetMeta(ctx context.Context, key string) (string, error) {
	var v string
	err := s.db.QueryRowContext(ctx, `SELECT value FROM meta WHERE key=?`, key).Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return v, err
}
