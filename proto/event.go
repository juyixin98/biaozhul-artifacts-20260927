package proto

import (
	"fmt"
	"time"
)

// EventType 标记事件日志中的因果动作。所有改变 DS 计数的动作
// （派生、认领、报告、重派、超时、终态）都落一条事件，使重放成为可能。
type EventType string

const (
	// EvJobCreated 作业创建，根任务入队。
	EvJobCreated EventType = "job.created"
	// EvTaskSpawned 父任务派生一个子任务：父 deficit++，子节点 engaged。
	EvTaskSpawned EventType = "task.spawned"
	// EvTaskClaimed 工作者认领任务（在途转移，不改变 DS 计数）。
	EvTaskClaimed EventType = "task.claimed"
	// EvTaskReported 任务完成被确认；其全部入边清偿后向父发信号。
	EvTaskReported EventType = "task.reported"
	// EvSignal 子节点向父节点清偿一条因果边（deficit--）。
	EvSignal EventType = "task.signal"
	// EvTaskRequeued 租约到期未确认，任务重新入队（在途保持）。
	EvTaskRequeued EventType = "task.requeued"
	// EvTaskTimedOut 预算到期仍未确认，任务永久未确认。
	EvTaskTimedOut EventType = "task.timed_out"
	// EvJobComplete 根判定终止：所有依赖清偿且无在途/未确认任务。
	EvJobComplete EventType = "job.complete"
	// EvJobFailed 预算到期仍有未确认任务。
	EvJobFailed EventType = "job.failed"
	// EvDuplicateIgnored 重复/过期提交被识别并忽略（计数不变）。
	EvDuplicateIgnored EventType = "task.duplicate_ignored"
)

// Event 是事件日志中的一条不可变记录。
type Event struct {
	Seq       int64          `json:"seq"`
	JobID     JobID          `json:"job_id"`
	Type      EventType      `json:"type"`
	TaskID    TaskID         `json:"task_id,omitempty"`
	ParentID  TaskID         `json:"parent_id,omitempty"`
	EdgeID    EdgeID         `json:"edge_id,omitempty"`
	LeaseID   LeaseID        `json:"lease_id,omitempty"`
	WorkerID  WorkerID       `json:"worker_id,omitempty"`
	RequestID string         `json:"request_id,omitempty"` // 关联 HTTP 请求身份
	Detail    map[string]any `json:"detail,omitempty"`
	OccurredAt time.Time    `json:"occurred_at"`
}

// FailureCategory 把失败原因分成可断言的确定类别，而不是笼统错误。
type FailureCategory string

const (
	// FailValidation 请求体/参数不合法（确定性拒绝，不入状态机）。
	FailValidation FailureCategory = "validation_error"
	// FailNotFound 作业或任务不存在。
	FailNotFound FailureCategory = "not_found"
	// FailUnknownLease 报告携带的 lease_id 与当前有效租约不符（迟到/伪造）。
	FailUnknownLease FailureCategory = "unknown_lease"
	// FailJobNotRunning 作业已 complete/failed，拒绝新的认领或报告。
	FailJobNotRunning FailureCategory = "job_not_running"
	// FailDuplicate 同一任务的重复确认；幂等忽略，不减少任何计数。
	FailDuplicate FailureCategory = "duplicate_ack"
	// FailBudgetExceeded 预算到期时仍有未确认任务。
	FailBudgetExceeded FailureCategory = "budget_exceeded"
	// FailUnacknowledged 任务在预算内始终未确认（作业失败的细粒度原因）。
	FailUnacknowledged FailureCategory = "unacknowledged"
	// FailReplayMismatch 重放与物化状态不一致（状态被外部篡改或存储损坏）。
	FailReplayMismatch FailureCategory = "replay_mismatch"
	// FailInternal 不应发生的内部不变量破坏。
	FailInternal FailureCategory = "internal_error"
)

// HTTPStatus 返回该失败类别对应的 HTTP 状态码。
func (c FailureCategory) HTTPStatus() int {
	switch c {
	case FailValidation:
		return 400
	case FailNotFound:
		return 404
	case FailUnknownLease, FailJobNotRunning:
		return 409
	case FailDuplicate:
		return 200 // 幂等语义：重复提交不是客户端错误
	case FailBudgetExceeded, FailUnacknowledged:
		return 408
	case FailReplayMismatch:
		return 409
	default:
		return 500
	}
}

// Error 是跨模块统一的可分类错误。
type Error struct {
	Category  FailureCategory `json:"category"`
	Message   string          `json:"message"`
	RequestID string          `json:"request_id,omitempty"`
	// Uncertain 为 true 时结论不确定（例如超时时刻与报告在途竞争），
	// 接口与日志会把不确定结论单列，绝不伪装成确定成功。
	Uncertain bool `json:"uncertain,omitempty"`
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	s := string(e.Category) + ": " + e.Message
	if e.RequestID != "" {
		s += " (request_id=" + e.RequestID + ")"
	}
	return s
}

// Fail 构造一个分类错误。
func Fail(cat FailureCategory, format string, args ...any) *Error {
	return &Error{Category: cat, Message: fmt.Sprintf(format, args...)}
}
