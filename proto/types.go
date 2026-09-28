// Package proto 定义异步任务网络中跨模块共享的协议类型：
// 任务、在途租约、事件、作业阶段、失败类别与分区键。
//
// 本包不包含任何业务逻辑，只规定数据形状与常量。
package proto

import "time"

// ID 是全局唯一身份标识。任务的每次转移（派生/认领）都携带唯一身份，
// 同一任务的重复确认凭 TaskID 幂等识别，不会减少两次计数。
type ID string

type JobID = ID
type TaskID = ID
type WorkerID = ID
type LeaseID = ID
type EdgeID = ID

// TaskKind 是计算内核支持的任务种类。
type TaskKind string

const (
	KindRoot      TaskKind = "root"      // 根任务：每个作业一个，不做实际计算
	KindFanout    TaskKind = "fanout"    // 派生子任务
	KindHashchain TaskKind = "hashchain" // 叶子：纯迭代哈希
)

// TaskStatus 是任务在 DS 状态机视角下的状态。
type TaskStatus string

const (
	// StatusReady 任务已派生（因果边已登记）但尚未被工作者认领：在途。
	StatusReady TaskStatus = "ready"
	// StatusClaimed 任务已被认领、租约有效：在途。
	StatusClaimed TaskStatus = "claimed"
	// StatusReported 任务已完成并被确认：节点被动且其因果边已清偿。
	StatusReported TaskStatus = "reported"
	// StatusTimedOut 预算内始终未确认：永久未确认，作业失败。
	StatusTimedOut TaskStatus = "timed_out"
)

// JobPhase 是作业生命周期阶段。
type JobPhase string

const (
	PhaseRunning  JobPhase = "running"  // 网络仍可能在工作
	PhaseComplete JobPhase = "complete" // DS 终止且无未完成任务
	PhaseFailed   JobPhase = "failed"   // 预算耗尽仍有未确认任务
)

// TaskSpec 是任务的不可变规格（派生时写入，随任务一起转移）。
type TaskSpec struct {
	JobID    JobID          `json:"job_id"`
	TaskID   TaskID         `json:"task_id"`
	ParentID TaskID         `json:"parent_id,omitempty"` // 根任务为空
	Kind     TaskKind       `json:"kind"`
	Depth    int            `json:"depth"`
	Payload  map[string]any `json:"payload,omitempty"`
}

// Task 是状态存储中一个任务节点的当前视图。
type Task struct {
	Spec       TaskSpec      `json:"spec"`
	Status     TaskStatus    `json:"status"`
	LeaseID    LeaseID       `json:"lease_id,omitempty"`
	WorkerID   WorkerID      `json:"worker_id,omitempty"`
	LeaseUntil time.Time     `json:"lease_until,omitempty"`
	CreatedAt  time.Time     `json:"created_at"`
	UpdatedAt  time.Time     `json:"updated_at"`
}

// Claim 是一次任务认领（在途租约）。
type Claim struct {
	TaskID      TaskID     `json:"task_id"`
	LeaseID     LeaseID    `json:"lease_id"`
	WorkerID    WorkerID   `json:"worker_id"`
	LeaseUntil  time.Time  `json:"lease_until"`
	Spec        TaskSpec   `json:"spec"`
}
