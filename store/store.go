// Package store 是 PostgreSQL 状态存储：物化任务/作业状态 + 仅追加事件日志。
//
// 一致性模型：每个作业的每次状态变更在单个数据库事务内完成，事务开始即取
// 该作业分区的事务级咨询锁（pg_advisory_xact_lock），使同一作业的 DS 计数
// 变更是全序的；落在不同分区的不同作业可以在不同连接上并行推进。
//
// 计数不自相矛盾地双写：spawned/signals/reports/dup/unack/inflight 全部
// 在加载时由 tasks/events/task_outputs 行物化推导，因此事件日志与物化
// 状态若出现分歧，重放核对必然能发现。
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"dsnet/kernel"
	"dsnet/proto"
)

//go:embed schema.sql
var schemaSQL string

// Config 是数据库连接配置（全部来自本地配置/环境变量）。
type Config struct {
	DSN             string
	MaxConns        int32
	AcquireTimeout  time.Duration
}

// Store 包装连接池。
type Store struct {
	pool *pgxpool.Pool
}

// Connect 建立连接池并校验连通性。
func Connect(ctx context.Context, cfg Config) (*Store, error) {
	if cfg.MaxConns <= 0 {
		cfg.MaxConns = 8
	}
	pcfg, err := pgxpool.ParseConfig(cfg.DSN)
	if err != nil {
		return nil, fmt.Errorf("parse DSN: %w", err)
	}
	pcfg.MaxConns = cfg.MaxConns
	pool, err := pgxpool.NewWithConfig(ctx, pcfg)
	if err != nil {
		return nil, fmt.Errorf("connect pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close 关闭连接池。
func (s *Store) Close() { s.pool.Close() }

// EnsureSchema 应用 schema（幂等）。
func (s *Store) EnsureSchema(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, schemaSQL)
	if err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

// ErrQueueEmpty 表示作业本地队列中没有可认领任务（不代表全局终止）。
var ErrQueueEmpty = errors.New("local queue empty")

// Mutate 在单个加锁事务内加载作业状态、应用归约函数、持久化决策并提交。
//
// fn 对 *kernel.State 是唯一的状态修改入口；它必须只调用状态机命令。
// fn 返回 kernel.ErrQueueEmpty（或包装）时事务回滚并原样返回该哨兵。
func (s *Store) Mutate(
	ctx context.Context,
	jobID proto.JobID,
	requestID string,
	fn func(st *kernel.State) (*kernel.Decision, error),
) (*kernel.Decision, kernel.Evidence, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, kernel.Evidence{}, fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", proto.AdvisoryLockKey(jobID)); err != nil {
		return nil, kernel.Evidence{}, fmt.Errorf("advisory lock: %w", err)
	}

	st, err := loadState(ctx, tx, jobID, time.Now)
	if err != nil {
		return nil, kernel.Evidence{}, err
	}
	d, err := fn(st)
	if err != nil {
		if errors.Is(err, kernel.ErrQueueEmpty) {
			return nil, kernel.Evidence{}, ErrQueueEmpty
		}
		return nil, kernel.Evidence{}, err
	}
	if d == nil {
		return nil, kernel.Evidence{}, proto.Fail(proto.FailInternal, "归约函数返回空决策")
	}

	if err := persistDecision(ctx, tx, st, d, requestID); err != nil {
		return nil, kernel.Evidence{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, kernel.Evidence{}, fmt.Errorf("commit: %w", err)
	}
	return d, st.Evidence(), nil
}
