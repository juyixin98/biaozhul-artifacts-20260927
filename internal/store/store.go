// Package store 提供基于 SQLite 的持久化实现。
//
// 控制器通过它自己声明的 Tx 接口访问数据，因此协调逻辑不绑定具体数据库；
// 但所有真实运行路径都使用本文件的 SQL 实现，不存在内存版替身充当核心。
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"rollingdeploy/internal/model"
)

// ErrNotFound 是统一的不存在错误。
var ErrNotFound = errors.New("store: not found")

// Store 管理 SQLite 连接。
type Store struct {
	db *sql.DB
}

// Open 打开（必要时创建）数据库并执行迁移。
func Open(path string) (*Store, error) {
	// _txlock 保证写事务串行，配合 busy_timeout 避免偶发锁错误。
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

// Close 关闭连接。
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate() error {
	_, err := s.db.Exec(schema)
	return err
}

const schema = `
CREATE TABLE IF NOT EXISTS apps (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    replicas   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    id         TEXT PRIMARY KEY,
    app_id     TEXT NOT NULL REFERENCES apps(id),
    version    TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_app ON revisions(app_id, created_at);
CREATE TABLE IF NOT EXISTS rollouts (
    id               TEXT PRIMARY KEY,
    app_id           TEXT NOT NULL REFERENCES apps(id),
    op               TEXT NOT NULL,
    revision_id      TEXT NOT NULL,
    prev_revision_id TEXT NOT NULL DEFAULT '',
    replicas         INTEGER NOT NULL,
    max_surge        INTEGER NOT NULL,
    max_unavailable  INTEGER NOT NULL,
    ready_threshold  INTEGER NOT NULL,
    failure_limit    INTEGER NOT NULL,
    progress_ticks   INTEGER NOT NULL,
    status           TEXT NOT NULL,
    failure_category TEXT NOT NULL DEFAULT '',
    failure_reason   TEXT NOT NULL DEFAULT '',
    failure_count    INTEGER NOT NULL DEFAULT 0,
    capacity_streak  INTEGER NOT NULL DEFAULT 0,
    ticks            INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    finished_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rollouts_app ON rollouts(app_id, created_at);
CREATE TABLE IF NOT EXISTS instances (
    id           TEXT PRIMARY KEY,
    app_id       TEXT NOT NULL REFERENCES apps(id),
    rollout_id   TEXT NOT NULL,
    revision_id  TEXT NOT NULL,
    proc_id      TEXT NOT NULL DEFAULT '',
    phase        TEXT NOT NULL,
    ready_streak INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_app ON instances(app_id, phase);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    rollout_id  TEXT NOT NULL DEFAULT '',
    app_id      TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    instance_id TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    snapshot    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_rollout ON events(rollout_id, id);
`

// dbTx 是 *sql.DB 与 *sql.Tx 的公共能力。
type dbTx interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

// Tx 是控制器/服务在单个事务内使用的持久化能力（结构匹配 controller.TxFace 的超集）。
type Tx interface {
	CreateApp(ctx context.Context, a *model.App) error
	AppGet(ctx context.Context, id string) (*model.App, error)
	CreateRevision(ctx context.Context, r *model.Revision) error
	RevisionGet(ctx context.Context, id string) (*model.Revision, error)
	CreateRollout(ctx context.Context, r *model.Rollout) error
	RolloutGet(ctx context.Context, id string) (*model.Rollout, error)
	RolloutUpdate(ctx context.Context, r *model.Rollout) error
	InstancesByApp(ctx context.Context, appID string, activeOnly bool) ([]*model.Instance, error)
	InstanceCreate(ctx context.Context, in *model.Instance) error
	InstanceUpdate(ctx context.Context, in *model.Instance) error
	InstanceAdoptByVersion(ctx context.Context, appID, targetVersion, newRevisionID, newRolloutID string) (int, error)
	EventInsert(ctx context.Context, e *model.Event) (int64, error)
}

type tx struct{ q dbTx }

// WithTx 以可序列化写事务执行 fn。控制器的一个完整滴答在同一事务内提交。
func (s *Store) WithTx(ctx context.Context, fn func(Tx) error) error {
	q, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	t := &tx{q: q}
	if err := fn(t); err != nil {
		_ = q.Rollback()
		return err
	}
	return q.Commit()
}

// DB 暴露非事务读操作的入口。
func (s *Store) DB() dbTx { return s.db }

func ts(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func pt(s string) (time.Time, error) {
	if s == "" {
		return time.Time{}, nil
	}
	return time.Parse(time.RFC3339Nano, s)
}

// ---------- Apps ----------

// CreateApp 插入应用。
func (s *Store) CreateApp(ctx context.Context, a *model.App) error {
	return insertApp(ctx, s.db, a)
}
func (t *tx) CreateApp(ctx context.Context, a *model.App) error { return insertApp(ctx, t.q, a) }

func insertApp(ctx context.Context, q dbTx, a *model.App) error {
	if a.CreatedAt.IsZero() {
		a.CreatedAt = time.Now()
	}
	_, err := q.ExecContext(ctx,
		`INSERT INTO apps(id,name,replicas,created_at) VALUES(?,?,?,?)`,
		a.ID, a.Name, a.Replicas, ts(a.CreatedAt))
	return err
}

func scanApp(row interface{ Scan(...any) error }) (*model.App, error) {
	var a model.App
	var created string
	if err := row.Scan(&a.ID, &a.Name, &a.Replicas, &created); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	t, err := pt(created)
	if err != nil {
		return nil, err
	}
	a.CreatedAt = t
	return &a, nil
}

const appCols = `SELECT id,name,replicas,created_at FROM apps `

// AppGet 按 ID 取应用。
func (s *Store) AppGet(ctx context.Context, id string) (*model.App, error) {
	return appGet(ctx, s.db, id)
}

func (t *tx) AppGet(ctx context.Context, id string) (*model.App, error) { return appGet(ctx, t.q, id) }

func appGet(ctx context.Context, q dbTx, id string) (*model.App, error) {
	return scanApp(q.QueryRowContext(ctx, appCols+`WHERE id=?`, id))
}

// AppGetByName 按名称取应用。
func (s *Store) AppGetByName(ctx context.Context, name string) (*model.App, error) {
	return appGetByName(ctx, s.db, name)
}

func appGetByName(ctx context.Context, q dbTx, name string) (*model.App, error) {
	return scanApp(q.QueryRowContext(ctx, appCols+`WHERE name=?`, name))
}

// ---------- Revisions ----------

// CreateRevision 插入版本记录。
func (s *Store) CreateRevision(ctx context.Context, r *model.Revision) error {
	return insertRevision(ctx, s.db, r)
}
func (t *tx) CreateRevision(ctx context.Context, r *model.Revision) error {
	return insertRevision(ctx, t.q, r)
}
func insertRevision(ctx context.Context, q dbTx, r *model.Revision) error {
	if r.CreatedAt.IsZero() {
		r.CreatedAt = time.Now()
	}
	_, err := q.ExecContext(ctx,
		`INSERT INTO revisions(id,app_id,version,source,created_at) VALUES(?,?,?,?,?)`,
		r.ID, r.AppID, r.Version, r.Source, ts(r.CreatedAt))
	return err
}

func scanRevision(row interface{ Scan(...any) error }) (*model.Revision, error) {
	var r model.Revision
	var created string
	if err := row.Scan(&r.ID, &r.AppID, &r.Version, &r.Source, &created); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	t, _ := pt(created)
	r.CreatedAt = t
	return &r, nil
}

const revCols = `SELECT id,app_id,version,source,created_at FROM revisions `

// RevisionGet 取版本。
func (s *Store) RevisionGet(ctx context.Context, id string) (*model.Revision, error) {
	return revisionGet(ctx, s.db, id)
}
func (t *tx) RevisionGet(ctx context.Context, id string) (*model.Revision, error) {
	return revisionGet(ctx, t.q, id)
}
func revisionGet(ctx context.Context, q dbTx, id string) (*model.Revision, error) {
	return scanRevision(q.QueryRowContext(ctx, revCols+`WHERE id=?`, id))
}

// ListRevisions 按时间顺序列出应用的全部版本（回退历史永不删除）。
func (s *Store) ListRevisions(ctx context.Context, appID string) ([]*model.Revision, error) {
	rows, err := s.db.QueryContext(ctx, revCols+`WHERE app_id=? ORDER BY created_at, id`, appID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Revision
	for rows.Next() {
		r, err := scanRevision(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// ---------- Rollouts ----------

// CreateRollout 插入发布记录。
func (s *Store) CreateRollout(ctx context.Context, r *model.Rollout) error {
	return createRollout(ctx, s.db, r)
}
func (t *tx) CreateRollout(ctx context.Context, r *model.Rollout) error {
	return createRollout(ctx, t.q, r)
}
func createRollout(ctx context.Context, q dbTx, r *model.Rollout) error {
	if r.CreatedAt.IsZero() {
		r.CreatedAt = time.Now()
	}
	_, err := q.ExecContext(ctx, `
INSERT INTO rollouts(id,app_id,op,revision_id,prev_revision_id,replicas,max_surge,max_unavailable,
    ready_threshold,failure_limit,progress_ticks,status,failure_category,failure_reason,
    failure_count,capacity_streak,ticks,created_at,finished_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		r.ID, r.AppID, r.Op, r.RevisionID, r.PrevRevisionID, r.Replicas, r.MaxSurge, r.MaxUnavailable,
		r.ReadyThreshold, r.FailureLimit, r.ProgressTicks, r.Status, r.FailureCategory, r.FailureReason,
		r.FailureCount, r.CapacityStreak, r.Ticks, ts(r.CreatedAt), "")
	return err
}

const rolloutCols = `SELECT id,app_id,op,revision_id,prev_revision_id,replicas,max_surge,max_unavailable,
    ready_threshold,failure_limit,progress_ticks,status,failure_category,failure_reason,
    failure_count,capacity_streak,ticks,created_at,COALESCE(finished_at,'') FROM rollouts `

func scanRollout(row interface{ Scan(...any) error }) (*model.Rollout, error) {
	var r model.Rollout
	var created, finished string
	if err := row.Scan(&r.ID, &r.AppID, &r.Op, &r.RevisionID, &r.PrevRevisionID, &r.Replicas,
		&r.MaxSurge, &r.MaxUnavailable, &r.ReadyThreshold, &r.FailureLimit, &r.ProgressTicks,
		&r.Status, &r.FailureCategory, &r.FailureReason, &r.FailureCount, &r.CapacityStreak,
		&r.Ticks, &created, &finished); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	t, _ := pt(created)
	r.CreatedAt = t
	if finished != "" {
		ft, _ := pt(finished)
		r.FinishedAt = &ft
	}
	return &r, nil
}

// RolloutGet 取发布。
func (s *Store) RolloutGet(ctx context.Context, id string) (*model.Rollout, error) {
	return rolloutGet(ctx, s.db, id)
}
func (t *tx) RolloutGet(ctx context.Context, id string) (*model.Rollout, error) {
	return rolloutGet(ctx, t.q, id)
}
func rolloutGet(ctx context.Context, q dbTx, id string) (*model.Rollout, error) {
	return scanRollout(q.QueryRowContext(ctx, rolloutCols+`WHERE id=?`, id))
}

// RolloutUpdate 持久化协调循环对发布状态的全部修改。
func (t *tx) RolloutUpdate(ctx context.Context, r *model.Rollout) error {
	var finished string
	if r.FinishedAt != nil {
		finished = ts(*r.FinishedAt)
	}
	res, err := t.q.ExecContext(ctx, `UPDATE rollouts SET
    status=?,failure_category=?,failure_reason=?,failure_count=?,capacity_streak=?,ticks=?,finished_at=?
    WHERE id=?`,
		r.Status, r.FailureCategory, r.FailureReason, r.FailureCount, r.CapacityStreak, r.Ticks,
		finished, r.ID)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return fmt.Errorf("%w: rollout %s", ErrNotFound, r.ID)
	}
	return nil
}

// ActiveRollout 返回应用当前在途发布；没有则返回 (nil,nil)。
func (s *Store) ActiveRollout(ctx context.Context, appID string) (*model.Rollout, error) {
	r, err := scanRollout(s.db.QueryRowContext(ctx,
		rolloutCols+`WHERE app_id=? AND status IN (?,?) ORDER BY created_at DESC, id DESC LIMIT 1`,
		appID, model.StatusPending, model.StatusRunning))
	if errors.Is(err, ErrNotFound) {
		return nil, nil
	}
	return r, err
}

// ListInFlightRollouts 列出全部在途发布（协调循环扫描用）。
func (s *Store) ListInFlightRollouts(ctx context.Context) ([]*model.Rollout, error) {
	rows, err := s.db.QueryContext(ctx,
		rolloutCols+`WHERE status IN (?,?) ORDER BY created_at, id`,
		model.StatusPending, model.StatusRunning)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Rollout
	for rows.Next() {
		r, err := scanRollout(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// ListRollouts 按时间顺序列出应用的发布历史。
func (s *Store) ListRollouts(ctx context.Context, appID string) ([]*model.Rollout, error) {
	rows, err := s.db.QueryContext(ctx,
		rolloutCols+`WHERE app_id=? ORDER BY created_at, id`, appID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Rollout
	for rows.Next() {
		r, err := scanRollout(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// LatestSucceededRolloutBefore 返回某次发布之前、目标版本不同的最近成功发布（回退目标）。
func (s *Store) LatestSucceededRolloutBefore(ctx context.Context, appID, beforeID string) (*model.Rollout, error) {
	r, err := scanRollout(s.db.QueryRowContext(ctx,
		rolloutCols+`WHERE app_id=? AND status=? AND id<>? ORDER BY created_at DESC, id DESC LIMIT 1`,
		appID, model.StatusSucceeded, beforeID))
	if errors.Is(err, ErrNotFound) {
		return nil, nil
	}
	return r, err
}

// ---------- Instances ----------

const instCols = `SELECT id,app_id,rollout_id,revision_id,COALESCE(proc_id,''),phase,ready_streak,created_at,updated_at FROM instances `

func scanInstance(row interface{ Scan(...any) error }) (*model.Instance, error) {
	var in model.Instance
	var c, u string
	if err := row.Scan(&in.ID, &in.AppID, &in.RolloutID, &in.RevisionID, &in.ProcID,
		&in.Phase, &in.ReadyStreak, &c, &u); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	in.CreatedAt, _ = pt(c)
	in.UpdatedAt, _ = pt(u)
	return &in, nil
}

func instances(ctx context.Context, q dbTx, appID string, activeOnly bool) ([]*model.Instance, error) {
	qry := instCols + `WHERE app_id=?`
	if activeOnly {
		qry += ` AND phase IN ('starting','ready','failed')`
	}
	qry += ` ORDER BY created_at, id`
	rows, err := q.QueryContext(ctx, qry, appID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Instance
	for rows.Next() {
		in, err := scanInstance(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, in)
	}
	return out, rows.Err()
}

// InstancesByApp 列出实例；activeOnly 时排除 terminated 审计行。
func (s *Store) InstancesByApp(ctx context.Context, appID string, activeOnly bool) ([]*model.Instance, error) {
	return instances(ctx, s.db, appID, activeOnly)
}
func (t *tx) InstancesByApp(ctx context.Context, appID string, activeOnly bool) ([]*model.Instance, error) {
	return instances(ctx, t.q, appID, activeOnly)
}

// InstanceCreate 插入实例。
func (s *Store) InstanceCreate(ctx context.Context, in *model.Instance) error {
	return instanceCreate(ctx, s.db, in)
}
func (t *tx) InstanceCreate(ctx context.Context, in *model.Instance) error {
	return instanceCreate(ctx, t.q, in)
}
func instanceCreate(ctx context.Context, q dbTx, in *model.Instance) error {
	now := time.Now()
	if in.CreatedAt.IsZero() {
		in.CreatedAt = now
	}
	in.UpdatedAt = now
	_, err := q.ExecContext(ctx, `
INSERT INTO instances(id,app_id,rollout_id,revision_id,proc_id,phase,ready_streak,created_at,updated_at)
VALUES(?,?,?,?,?,?,?,?,?)`,
		in.ID, in.AppID, in.RolloutID, in.RevisionID, in.ProcID, in.Phase, in.ReadyStreak,
		ts(in.CreatedAt), ts(in.UpdatedAt))
	return err
}

// InstanceUpdate 更新实例阶段/就绪连续计数。
func (t *tx) InstanceUpdate(ctx context.Context, in *model.Instance) error {
	in.UpdatedAt = time.Now()
	res, err := t.q.ExecContext(ctx,
		`UPDATE instances SET revision_id=?,rollout_id=?,proc_id=?,phase=?,ready_streak=?,updated_at=? WHERE id=?`,
		in.RevisionID, in.RolloutID, in.ProcID, in.Phase, in.ReadyStreak, ts(in.UpdatedAt), in.ID)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return fmt.Errorf("%w: instance %s", ErrNotFound, in.ID)
	}
	return nil
}

// InstanceAdoptByVersion 把所有运行在目标版本上的活跃实例划归到新的（回退）revision/rollout。
// 返回被采纳的实例数。按“镜像版本”匹配，因此回退到 v1 时现存 v1 进程直接继续使用。
func (t *tx) InstanceAdoptByVersion(ctx context.Context, appID, targetVersion, newRevisionID, newRolloutID string) (int, error) {
	res, err := t.q.ExecContext(ctx, `
UPDATE instances SET revision_id=?, rollout_id=?, updated_at=?
WHERE app_id=? AND phase IN ('starting','ready','failed')
  AND revision_id IN (SELECT id FROM revisions WHERE app_id=? AND version=?)`,
		newRevisionID, newRolloutID, ts(time.Now()), appID, appID, targetVersion)
	if err != nil {
		return 0, err
	}
	n, err := res.RowsAffected()
	return int(n), err
}

// ---------- Events ----------

// EventInsert 写入一步发布历史（携带操作后快照）。
func (t *tx) EventInsert(ctx context.Context, e *model.Event) (int64, error) {
	if e.CreatedAt.IsZero() {
		e.CreatedAt = time.Now()
	}
	snap, err := json.Marshal(e.Snapshot)
	if err != nil {
		return 0, err
	}
	res, err := t.q.ExecContext(ctx, `
INSERT INTO events(request_id,rollout_id,app_id,kind,revision_id,instance_id,note,snapshot,created_at)
VALUES(?,?,?,?,?,?,?,?,?)`,
		e.RequestID, e.RolloutID, e.AppID, e.Kind, e.RevisionID, e.InstanceID, e.Note,
		string(snap), ts(e.CreatedAt))
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

func scanEvent(row interface{ Scan(...any) error }) (model.Event, error) {
	var e model.Event
	var snap, created string
	if err := row.Scan(&e.ID, &e.RequestID, &e.RolloutID, &e.AppID, &e.Kind, &e.RevisionID,
		&e.InstanceID, &e.Note, &snap, &created); err != nil {
		return e, err
	}
	if snap != "" {
		_ = json.Unmarshal([]byte(snap), &e.Snapshot)
	}
	e.CreatedAt, _ = pt(created)
	return e, nil
}

// ListEventsByRollout 按顺序返回某次发布的全部步骤（独立测试逐步断言用）。
func (s *Store) ListEventsByRollout(ctx context.Context, rolloutID string) ([]model.Event, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT id,request_id,rollout_id,app_id,kind,revision_id,instance_id,note,snapshot,created_at
FROM events WHERE rollout_id=? ORDER BY id`, rolloutID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Event
	for rows.Next() {
		e, err := scanEvent(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ListEventsByApp 返回应用聚合事件流。
func (s *Store) ListEventsByApp(ctx context.Context, appID string) ([]model.Event, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT id,request_id,rollout_id,app_id,kind,revision_id,instance_id,note,snapshot,created_at
FROM events WHERE app_id=? ORDER BY id`, appID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Event
	for rows.Next() {
		e, err := scanEvent(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}
