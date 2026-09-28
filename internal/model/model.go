// Package model 定义滚动发布控制器的核心资源模型。
//
// 这里只放纯数据类型、状态常量和与存储无关的派生计算，
// 不包含协调逻辑，保证协调循环可以针对接口编程、独立测试。
package model

import "time"

// 实例生命周期阶段。
const (
	PhaseStarting   = "starting"   // 已向进程管理器发起 Start，尚未达到就绪阈值
	PhaseReady      = "ready"      // 就绪探针连续成功达到阈值，计入可用
	PhaseFailed     = "failed"     // 启动失败 / 运行中崩溃，绝不计入可用
	PhaseTerminated = "terminated" // 已请求进程管理器 Stop，仅保留审计行
)

// IsActive 报告实例是否仍占用容量（terminated 行只作审计，不占容量）。
func IsActive(phase string) bool {
	switch phase {
	case PhaseStarting, PhaseReady, PhaseFailed:
		return true
	default:
		return false
	}
}

// 发布操作类型。
const (
	OpCreate   = "create"   // 应用首个版本上线
	OpRollout  = "rollout"  // 滚动更新到新版本
	OpRollback = "rollback" // 回退：也是一次全新的发布操作，历史不被覆盖
)

// 发布状态。
const (
	StatusPending   = "pending" // 已创建，尚未开始协调
	StatusRunning   = "running" // 协调中
	StatusSucceeded = "succeeded"
	StatusFailed    = "failed" // 失败被终止，等待人工回退或自动回退
)

// 失败类别（失败实例与不确定结论分列）。
const (
	FailStartFailure         = "start_failure"         // 新实例启动报错或启动后崩溃（确定的实例级失败）
	FailInsufficientCapacity = "insufficient_capacity" // 容量池耗尽且无法继续（确定的环境约束）
	FailReadinessTimeout     = "readiness_timeout"     // 超时仍未达到就绪阈值（不确定结论：可能稍后会好）
	FailInvalidStrategy      = "invalid_strategy"      // 策略导致零推进死锁（确定的配置错误）
)

// 模拟器夹具行为类型（本地合成依赖）。
const (
	BehaviorAlwaysOK   = "always_ok"   // 启动成功，探针始终就绪
	BehaviorFailStart  = "fail_start"  // 启动即返回错误
	BehaviorCrashAfter = "crash_after" // 启动成功，N 次探针后崩溃
	BehaviorFlaky      = "flaky"       // 探针周期性抖动：每个第 N 次检查失败
	BehaviorFlakyFirst = "flaky_first" // 前 N 次探针失败，之后稳定就绪
)

// App 是被发布的工作负载。
type App struct {
	ID        string
	Name      string
	Replicas  int // 期望就绪副本数 D
	CreatedAt time.Time
}

// Revision 是不可变版本记录。回退也会生成新的 revision，旧记录永不覆盖。
type Revision struct {
	ID        string
	AppID     string
	Version   string // 镜像/版本标签，模拟器据此选择夹具行为
	Source    string // "deploy" | "rollback:<fromRolloutID>"
	CreatedAt time.Time
}

// Rollout 描述一次发布操作及其约束与结果。
type Rollout struct {
	ID              string
	AppID           string
	Op              string
	RevisionID      string // 目标新版本
	PrevRevisionID  string // 上一版本（可能为空，表示 create）
	Replicas        int    // 本次期望副本（快照，防止后续修改 App 造成歧义）
	MaxSurge        int
	MaxUnavailable  int
	ReadyThreshold  int // 就绪探针需连续成功的次数
	FailureLimit    int // 新实例启动/崩溃失败多少次即判定失败
	ProgressTicks   int // 以协调滴答计的进度截止时间；0 表示不检查
	Status          string
	FailureCategory string // 仅 Status=failed 时有值
	FailureReason   string // 人类可读的失败细节
	FailureCount    int    // 本周期内启动/崩溃失败累计（持久化，重启不丢）
	CapacityStreak  int    // 容量拒绝连续计数（持久化）
	Ticks           int    // 已执行滴答数
	CreatedAt       time.Time
	FinishedAt      *time.Time
}

// InFlight 报告发布是否仍在推进。
func (r *Rollout) InFlight() bool { return r.Status == StatusPending || r.Status == StatusRunning }

// Instance 是一个模拟工作进程在控制器侧的记录。
type Instance struct {
	ID          string
	AppID       string
	RolloutID   string // 把它带起来的那次发布
	RevisionID  string
	ProcID      string // 模拟器中的进程标识（"app@version" 作用域 + 短 id）
	Phase       string
	ReadyStreak int // 连续就绪检查成功次数（持久化，重启后重新从探针累计）
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// Event 记录发布历史中的一个步骤。
//
// 每个写操作携带操作前后快照、触发它的请求身份与处理位置，
// 独立测试据此逐步断言副本约束；失败原因与普通步骤分开记录。
type Event struct {
	ID         int64
	RequestID  string // 关联 API 请求；协调循环自身触发时为 "tick:<rolloutID>"
	RolloutID  string
	AppID      string
	Kind       string // 见下方事件类型常量
	RevisionID string
	InstanceID string
	Note       string
	Snapshot   Snapshot // 该步骤应用后的容量快照
	CreatedAt  time.Time
}

// 事件类型。
const (
	EvTickBegin     = "tick_begin"     // 滴答开始（前置不变量快照）
	EvStartNew      = "start_new"      // 创建新实例（注意：创建 != 就绪）
	EvStartRejected = "start_rejected" // 容量不足，创建被拒绝
	EvStartFailed   = "start_failed"   // 创建动作返回错误或新实例崩溃
	EvBecomeReady   = "become_ready"   // 连续探针成功达到阈值
	EvRemoveOld     = "remove_old"     // 缩容旧版本
	EvReattach      = "reattach"       // 控制器重启后重新同步模拟器状态
	EvProbeDemoted  = "ready_demoted"  // 曾经就绪的实例探针再次失败，移出可用计数
	EvRolloutDone   = "rollout_succeeded"
	EvRolloutFail   = "rollout_failed" // 失败结论（category 单列）
)

// Snapshot 是某一步应用后的容量视图，全部为具体计数，供逐步断言。
type Snapshot struct {
	TotalActive  int `json:"total_active"`  // 占用容量的实例总数（含 starting/failed）
	NewActive    int `json:"new_active"`    // 其中新版本活跃数
	OldActive    int `json:"old_active"`    // 其中旧版本活跃数
	NewAvailable int `json:"new_available"` // 新版本就绪数（达到阈值才算）
	OldAvailable int `json:"old_available"` // 旧版本就绪数
	Available    int `json:"available"`     // 总可用（failed/starting 绝不计入）
	Desired      int `json:"desired"`       // D
	MaxTotal     int `json:"max_total"`     // D + maxSurge
	MinAvailable int `json:"min_available"` // D - maxUnavailable
	Tick         int `json:"tick"`          // 发布内第几个滴答（从 1 开始）
}

// ComputeSnapshot 依据实例列表与目标版本计算容量快照。
// 阈值就绪判断：只有 ready 阶段计入可用；starting/failed 明确排除。
func ComputeSnapshot(instances []*Instance, targetRevID string, desired, surge, unavail, tick int) Snapshot {
	s := Snapshot{Desired: desired, MaxTotal: desired + surge, MinAvailable: desired - unavail, Tick: tick}
	for _, in := range instances {
		if !IsActive(in.Phase) {
			continue
		}
		s.TotalActive++
		if in.RevisionID == targetRevID {
			s.NewActive++
			if in.Phase == PhaseReady {
				s.NewAvailable++
			}
		} else {
			s.OldActive++
			if in.Phase == PhaseReady {
				s.OldAvailable++
			}
		}
	}
	s.Available = s.NewAvailable + s.OldAvailable
	return s
}
