// Package store 定义重组状态的持久化抽象，并提供内存实现与 SQLite 实现。
//
// 重组器自身持有 pending 组的权威内存状态；Store 承担三件事：
//  1. 崩溃/重启后把未终结组及分片恢复回重组器；
//  2. 审计：记录每片接受/重复事实与组的状态迁移；
//  3. 终结结果留存：在结果 TTL 内支持 Lookup 查询，到期彻底回收。
package store

import (
	"context"
	"time"

	"ipfragreasm/internal/netmodel"
)

// 组状态以裸字符串保存，避免 store 与 reasm 形成导入环。
// 取值须与 reasm.State 的字符串常量一致。
const (
	StatePending  = "pending"
	StateComplete = "complete"
	StateRejected = "rejected"
	StateTimedOut = "timed_out"
)

// IsTerminalState 报告状态字符串是否为终结态。
func IsTerminalState(s string) bool {
	return s == StateComplete || s == StateRejected || s == StateTimedOut
}

// GroupRecord 是 groups 表的一行。
type GroupRecord struct {
	Key         netmodel.FragKey
	State       string
	StartedAt   time.Time
	Deadline    time.Time
	TerminalAt  time.Time
	ExpiresAt   time.Time
	HasLast     bool
	LastOffset  int
	TotalLength int
	Reason      string
	Assembled   []byte
}

// FragmentRecord 是 fragments 表的一行（仅保存属于未终结组的活动分片）。
type FragmentRecord struct {
	Key       netmodel.FragKey
	Seq       int
	Offset    int
	Length    int
	More      bool
	Payload   []byte
	Duplicate bool
	SeenAt    time.Time
}

// Store 为重组器需要的全部状态操作。
type Store interface {
	// UpsertGroup 新建或覆盖组记录。
	UpsertGroup(ctx context.Context, g GroupRecord) error
	// GetGroup 读取组记录；不存在返回 (zero,false,nil)。
	GetGroup(ctx context.Context, key netmodel.FragKey) (GroupRecord, bool, error)
	// DeleteGroup 同时删除组与其分片行（彻底回收）。
	DeleteGroup(ctx context.Context, key netmodel.FragKey) error

	// AddFragment 追加一片。seq 由调用方按组内序号给出。
	AddFragment(ctx context.Context, f FragmentRecord) error
	// ListFragments 按 seq 升序返回组的全部分片。
	ListFragments(ctx context.Context, key netmodel.FragKey) ([]FragmentRecord, error)
	// DeleteFragments 清空组的分片行（组终结时调用，组行保留到留存到期）。
	DeleteFragments(ctx context.Context, key netmodel.FragKey) error

	// ListOpenGroups 返回所有非终结组（重启恢复用）。
	ListOpenGroups(ctx context.Context) ([]GroupRecord, error)
	// ListTerminalExpired 返回 ExpiresAt <= now 的终结组（后台回收用）。
	ListTerminalExpired(ctx context.Context, now time.Time) ([]GroupRecord, error)
	// ListAllGroups 用于统计与诊断。
	ListAllGroups(ctx context.Context) ([]GroupRecord, error)
	// CountFragments 返回分片总行数。
	CountFragments(ctx context.Context) (int, error)

	Close() error
}
