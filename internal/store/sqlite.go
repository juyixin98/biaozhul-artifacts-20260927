// Package store 是 SQLite 持久化适配器：保存标签快照、策略集合与判定记录。
//
// 它只做持久化与读取，不做策略求值；引擎从 Reconciler 得到当前生效的
// （快照, 策略集合）视图。快照与策略集合整体替换，写库用单事务保证原子性。
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"netpolreach/internal/model"
)

// 故障类别：上层（HTTP/故障测试）据此把存储错误映射为确定的错误码。
var (
	ErrNotFound         = errors.New("store: 记录不存在")
	ErrAlreadyExists    = errors.New("store: 记录已存在")
	ErrVersionConflict  = errors.New("store: 版本冲突")
	ErrCorrupt          = errors.New("store: 数据损坏")
	ErrUnavailable      = errors.New("store: 存储不可用")
)

// Store 是存储端口接口。生产实现是 SQLiteStore；
// 测试可用 FaultyStore 注入故障。
type Store interface {
	SaveSnapshot(ctx context.Context, snap model.Snapshot, overwrite bool) error
	LoadSnapshot(ctx context.Context) (model.Snapshot, error)
	HasSnapshot(ctx context.Context) (bool, error)
	SavePolicySet(ctx context.Context, ps model.PolicySet, overwrite bool) error
	LoadPolicySet(ctx context.Context) (model.PolicySet, error)
	HasPolicySet(ctx context.Context) (bool, error)
	InsertDecision(ctx context.Context, rec DecisionRecord) error
	ListDecisions(ctx context.Context, limit int) ([]DecisionRecord, error)
	Ping(ctx context.Context) error
	Close() error
}

// DecisionRecord 是一次判定的持久化诊断记录。PodIP 等敏感字段不落库。
type DecisionRecord struct {
	ID            int64     `json:"id"`
	RequestID     string    `json:"request_id"`
	CreatedAt     time.Time `json:"created_at"`
	LabelVersion  string    `json:"label_version"`
	PolicyVersion string    `json:"policy_version"`
	ProbeKey      string    `json:"probe_key"`
	Verdict       string    `json:"verdict"`
	Reason        string    `json:"reason"`
	PayloadJSON   string    `json:"-"` // 完整判定体（不含 pod_ip）
}

// SQLiteStore 是基于 modernc.org/sqlite 的纯 Go 实现（无 CGO）。
type SQLiteStore struct {
	db *sql.DB
}

// NewSQLiteStore 打开（不存在则创建）数据库并执行迁移。
func NewSQLiteStore(dsn string) (*SQLiteStore, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	// 单写者，配合事务即可避免 database is locked。
	if _, err := db.Exec(`PRAGMA busy_timeout = 5000; PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;`); err != nil {
		db.Close()
		return nil, fmt.Errorf("%w: pragmas: %v", ErrUnavailable, err)
	}
	s := &SQLiteStore{db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLiteStore) migrate() error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS meta(
			k TEXT PRIMARY KEY,
			v TEXT NOT NULL,
			updated_at TEXT NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS snapshots(
			id INTEGER PRIMARY KEY CHECK (id = 1),
			label_version TEXT NOT NULL,
			policy_version TEXT NOT NULL,
			payload TEXT NOT NULL,
			updated_at TEXT NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS policy_sets(
			id INTEGER PRIMARY KEY CHECK (id = 1),
			version TEXT NOT NULL,
			payload TEXT NOT NULL,
			updated_at TEXT NOT NULL
		);`,
		`CREATE TABLE IF NOT EXISTS decisions(
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			request_id TEXT NOT NULL,
			created_at TEXT NOT NULL,
			label_version TEXT NOT NULL,
			policy_version TEXT NOT NULL,
			probe_key TEXT NOT NULL,
			verdict TEXT NOT NULL,
			reason TEXT NOT NULL,
			payload TEXT NOT NULL
		);`,
		`CREATE INDEX IF NOT EXISTS idx_decisions_request ON decisions(request_id);`,
		`CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);`,
	}
	for _, st := range stmts {
		if _, err := s.db.Exec(st); err != nil {
			return fmt.Errorf("%w: migration: %v", ErrCorrupt, err)
		}
	}
	return nil
}

// Ping 用于健康检查；若底层错误则归类为不可用。
func (s *SQLiteStore) Ping(ctx context.Context) error {
	if err := s.db.PingContext(ctx); err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT 1`).Scan(&n); err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	return nil
}

// SaveSnapshot 整体写入快照。overwrite=false 且已有快照时返回 ErrAlreadyExists。
func (s *SQLiteStore) SaveSnapshot(ctx context.Context, snap model.Snapshot, overwrite bool) error {
	payload, err := json.Marshal(snap)
	if err != nil {
		return fmt.Errorf("%w: 序列化快照: %v", ErrCorrupt, err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	defer tx.Rollback()

	var exists int
	if err := tx.QueryRowContext(ctx, `SELECT COUNT(1) FROM snapshots WHERE id=1`).Scan(&exists); err != nil {
		return fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	if exists == 1 && !overwrite {
		return ErrAlreadyExists
	}
	if exists == 1 {
		_, err = tx.ExecContext(ctx,
			`UPDATE snapshots SET label_version=?, policy_version=?, payload=?, updated_at=? WHERE id=1`,
			snap.LabelVersion, snap.PolicyVersion, string(payload), now)
	} else {
		_, err = tx.ExecContext(ctx,
			`INSERT INTO snapshots(id,label_version,policy_version,payload,updated_at) VALUES(1,?,?,?,?)`,
			snap.LabelVersion, snap.PolicyVersion, string(payload), now)
	}
	if err != nil {
		return fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("%w: commit: %v", ErrUnavailable, err)
	}
	return nil
}

// LoadSnapshot 读取当前快照；尚未装载时返回 ErrNotFound。
func (s *SQLiteStore) LoadSnapshot(ctx context.Context) (model.Snapshot, error) {
	var payload string
	err := s.db.QueryRowContext(ctx, `SELECT payload FROM snapshots WHERE id=1`).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		return model.Snapshot{}, ErrNotFound
	}
	if err != nil {
		return model.Snapshot{}, fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	var snap model.Snapshot
	if err := json.Unmarshal([]byte(payload), &snap); err != nil {
		return model.Snapshot{}, fmt.Errorf("%w: 快照 JSON 不可解析: %v", ErrCorrupt, err)
	}
	return snap, nil
}

func (s *SQLiteStore) HasSnapshot(ctx context.Context) (bool, error) {
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(1) FROM snapshots WHERE id=1`).Scan(&n); err != nil {
		return false, fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	return n == 1, nil
}

// SavePolicySet 整体写入策略集合。
func (s *SQLiteStore) SavePolicySet(ctx context.Context, ps model.PolicySet, overwrite bool) error {
	payload, err := json.Marshal(ps)
	if err != nil {
		return fmt.Errorf("%w: 序列化策略集合: %v", ErrCorrupt, err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	defer tx.Rollback()

	var exists int
	if err := tx.QueryRowContext(ctx, `SELECT COUNT(1) FROM policy_sets WHERE id=1`).Scan(&exists); err != nil {
		return fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	if exists == 1 && !overwrite {
		return ErrAlreadyExists
	}
	if exists == 1 {
		_, err = tx.ExecContext(ctx,
			`UPDATE policy_sets SET version=?, payload=?, updated_at=? WHERE id=1`,
			ps.Version, string(payload), now)
	} else {
		_, err = tx.ExecContext(ctx,
			`INSERT INTO policy_sets(id,version,payload,updated_at) VALUES(1,?,?,?)`,
			ps.Version, string(payload), now)
	}
	if err != nil {
		return fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("%w: commit: %v", ErrUnavailable, err)
	}
	return nil
}

func (s *SQLiteStore) LoadPolicySet(ctx context.Context) (model.PolicySet, error) {
	var payload string
	err := s.db.QueryRowContext(ctx, `SELECT payload FROM policy_sets WHERE id=1`).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		return model.PolicySet{}, ErrNotFound
	}
	if err != nil {
		return model.PolicySet{}, fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	var ps model.PolicySet
	if err := json.Unmarshal([]byte(payload), &ps); err != nil {
		return model.PolicySet{}, fmt.Errorf("%w: 策略集合 JSON 不可解析: %v", ErrCorrupt, err)
	}
	return ps, nil
}

func (s *SQLiteStore) HasPolicySet(ctx context.Context) (bool, error) {
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(1) FROM policy_sets WHERE id=1`).Scan(&n); err != nil {
		return false, fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	return n == 1, nil
}

// InsertDecision 记录一条脱敏后的判定诊断。
func (s *SQLiteStore) InsertDecision(ctx context.Context, rec DecisionRecord) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO decisions(request_id,created_at,label_version,policy_version,probe_key,verdict,reason,payload)
		 VALUES(?,?,?,?,?,?,?,?)`,
		rec.RequestID,
		rec.CreatedAt.UTC().Format(time.RFC3339Nano),
		rec.LabelVersion,
		rec.PolicyVersion,
		rec.ProbeKey,
		rec.Verdict,
		rec.Reason,
		rec.PayloadJSON)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	return nil
}

func (s *SQLiteStore) ListDecisions(ctx context.Context, limit int) ([]DecisionRecord, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT id,request_id,created_at,label_version,policy_version,probe_key,verdict,reason
		 FROM decisions ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrCorrupt, err)
	}
	defer rows.Close()
	var out []DecisionRecord
	for rows.Next() {
		var r DecisionRecord
		var ts string
		if err := rows.Scan(&r.ID, &r.RequestID, &ts, &r.LabelVersion, &r.PolicyVersion,
			&r.ProbeKey, &r.Verdict, &r.Reason); err != nil {
			return nil, fmt.Errorf("%w: %v", ErrCorrupt, err)
		}
		r.CreatedAt, _ = time.Parse(time.RFC3339Nano, ts)
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) Close() error { return s.db.Close() }
