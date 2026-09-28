package coordinator

import (
	"time"

	"example.com/cgcoord/protocol"
)

// CreateGroupRequest creates a group and its fixed topic universe.
type CreateGroupRequest struct {
	Name    string
	Topics  []protocol.TopicSpec
	Config  protocol.GroupConfig
	RequestID string
	At      time.Time
}

// JoinRequest adds a member.
type JoinRequest struct {
	Group       string
	Member      protocol.MemberSpec
	RequestID   string
	At          time.Time
}

// JoinResult reports what the join triggered.
type JoinResult struct {
	Member          protocol.MemberID
	Phase           protocol.Phase
	Generation      protocol.Generation
	// Revocations lists the partitions this member has been asked to revoke
	// (non-empty only when the new member displaces it in the plan; an
	// ordinary join usually leaves existing owners untouched).
	Revocations []protocol.TP
}

// LeaveRequest removes a member in order. Its owned partitions are revoked
// and immediately confirmed, because an orderly leave is itself the release.
type LeaveRequest struct {
	Group     string
	Member    protocol.MemberID
	RequestID string
	At        time.Time
}

// LeaveResult reports the generation the leave kicked off.
type LeaveResult struct {
	Phase      protocol.Phase
	Generation protocol.Generation
}

// HeartbeatRequest refreshes liveness and returns the member's current
// obligations.
type HeartbeatRequest struct {
	Group         string
	Member        protocol.MemberID
	Generation    protocol.Generation
	RequestID     string
	At            time.Time
}

// HeartbeatResult is what the member needs to act on.
type HeartbeatResult struct {
	Phase           protocol.Phase
	Generation      protocol.Generation
	Revoke          []protocol.TP // revoke these, then call RevokeAck
	Owned           []protocol.TP // stable generation: effective ownership
	Quarantined     []protocol.TP // partitions with no effective owner
}

// SyncRequest lets a member wait/observe activation of the generation it
// joined or was revoked for.
type SyncRequest struct {
	Group       string
	Member      protocol.MemberID
	Generation  protocol.Generation
	RequestID   string
}

// SyncResult is the member's assignment once its generation is stable.
type SyncResult struct {
	Generation protocol.Generation
	Assignment []protocol.TP
	Stable     bool
	Phase      protocol.Phase
}

// AckRequest confirms revocations.
type AckRequest struct {
	Group     string
	Member    protocol.MemberID
	Generation protocol.Generation
	Partitions []protocol.TP
	RequestID string
	At        time.Time
}

// AckItem is the per-partition outcome of a revocation confirmation.
type AckItem struct {
	TP       protocol.TP
	Outcome  string // acked | recovered | stale | unknown | quarantined
	Reason   string
}

// AckResult categorizes every partition in the request.
type AckResult struct {
	Phase           protocol.Phase
	Generation      protocol.Generation
	Activated       bool
	Acked           []AckItem
	Skipped         []AckItem
}

// RecoverAckRequest is an out-of-band confirmation for a quarantined
// partition, used by tooling after the old owner is known to have stopped.
type RecoverAckRequest struct {
	Group     string
	Member    protocol.MemberID // may be empty: recovery tool, not the owner
	Generation protocol.Generation
	Partitions []protocol.TP
	Reason    string
	RequestID string
	At        time.Time
}

// CommitItem is one offset in a batch commit.
type CommitItem struct {
	TP          protocol.TP
	Offset      int64
	LeaderEpoch int64
}

// CommitResultItem reports one item outcome.
type CommitResultItem struct {
	TP       protocol.TP
	Offset   int64
	Committed bool
	Code     protocol.ErrorCode
	Message  string
}

// CommitRequest submits offsets. Items are fenced individually: one stale
// item does not poison the batch, but the response lists it as rejected.
type CommitRequest struct {
	Group      string
	Member     protocol.MemberID
	Generation protocol.Generation
	Items      []CommitItem
	RequestID  string
	At         time.Time
}

// CommitResult reports every item.
type CommitResult struct {
	Generation protocol.Generation
	Items      []CommitResultItem
}

// FetchOffsetsRequest reads committed offsets.
type FetchOffsetsRequest struct {
	Group      string
	Partitions []protocol.TP // empty means all
}

// StateView is the externally exposed snapshot. It is built from the cloned
// internal state plus derived diagnostics (uncertainties).
type StateView struct {
	Name       string                          `json:"name"`
	Generation protocol.Generation             `json:"generation"`
	Phase      protocol.Phase                  `json:"phase"`
	Topics     []protocol.TopicSpec            `json:"topics"`
	Members    []MemberView                    `json:"members"`
	Partitions []PartitionView                 `json:"partitions"`
	Uncertainties []string                     `json:"uncertainties"`
}

// MemberView is one member in StateView.
type MemberView struct {
	ID            protocol.MemberID         `json:"id"`
	State         protocol.MemberState      `json:"state"`
	SubscribesTo  []protocol.Topic          `json:"subscribes_to"`
	Owned         []protocol.TP             `json:"owned"`
	Revoking      []protocol.TP             `json:"revoking"`
	LastHeartbeat time.Time                 `json:"last_heartbeat"`
}

// PartitionView is one partition in StateView.
type PartitionView struct {
	TP        protocol.TP            `json:"tp"`
	State     protocol.PartitionState `json:"state"`
	Owner     *protocol.MemberID     `json:"owner,omitempty"`
	Generation protocol.Generation   `json:"generation,omitempty"`
	Detail    string                 `json:"detail,omitempty"`
}
