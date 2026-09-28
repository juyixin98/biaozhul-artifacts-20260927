package reasm

import (
	"time"

	"ipfragreasm/internal/netmodel"
)

// State 是重组组的显式状态机取值。
type State string

const (
	// StatePending：已收到部分片，未看到末片或仍存在缺口，禁止提前输出。
	StatePending State = "pending"
	// StateComplete：末片已到且偏移空间连续覆盖 [0,totalLen)，重组成功。
	StateComplete State = "complete"
	// StateRejected：因重叠/冲突末片/超长被整组拒绝。
	StateRejected State = "rejected"
	// StateTimedOut：超过明确超时仍未完成，组被回收，ID 可复用。
	StateTimedOut State = "timed_out"
)

// IsTerminal 报告状态是否终结。
func (s State) IsTerminal() bool {
	return s == StateComplete || s == StateRejected || s == StateTimedOut
}

// Progress 描述当前重组进度，供日志显示计算步骤与判定依据。
type Progress struct {
	// Received 为已接受（不含重复）的分片数。
	Received int `json:"received"`
	// Duplicates 为识别出的完全重复片次数。
	Duplicates int `json:"duplicates"`
	// HasLast 为是否已收到末片。
	HasLast bool `json:"has_last"`
	// LastOffset 为末片起始字节偏移；HasLast=false 时为 nil。
	LastOffset *int `json:"last_offset,omitempty"`
	// TotalLength 为末片宣告的总载荷长度；HasLast=false 时为 0。
	TotalLength int `json:"total_length,omitempty"`
	// Covered 为当前无空洞可确认覆盖到的最远字节位置。
	Covered int `json:"covered"`
}

// Outcome 是 Process/Sweep 的结构化结果，明确区分接受、完全重复、拒绝、超时。
type Outcome struct {
	Key       netmodel.FragKey `json:"-"`
	KeyText   string           `json:"key"`
	State     State            `json:"state"`
	Accepted  bool             `json:"accepted"`
	Duplicate bool             `json:"duplicate"`
	// Recycled 表示该组在本次动作中被彻底回收（终结留存到期删除）。
	Recycled bool `json:"recycled"`
	// Reason 在 Rejected 时给出 ErrorKind；TimedOut 为 "timeout"。
	Reason string `json:"reason,omitempty"`
	Detail string `json:"detail,omitempty"`

	Progress Progress `json:"progress"`

	// Assembled 仅在 State=Complete 时给出重组字节。
	Assembled []byte `json:"assembled,omitempty"`

	StartedAt time.Time  `json:"started_at"`
	Deadline  time.Time  `json:"deadline"`
	ExpiresAt *time.Time `json:"expires_at,omitempty"`
}

// Snapshot 是 Lookup 返回的只读视图。
type Snapshot struct {
	Key       netmodel.FragKey
	KeyText   string
	State     State
	Progress  Progress
	Reason    string
	Assembled []byte
	StartedAt time.Time
	Deadline  time.Time
	ExpiresAt *time.Time
}

// Stats 暴露资源占用，供回收测试断言。
type Stats struct {
	ActivePending  int `json:"active_pending"`
	StoreGroups    int `json:"store_groups"`
	StoreFragments int `json:"store_fragments"`
}
