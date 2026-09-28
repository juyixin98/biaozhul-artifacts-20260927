// Package protocol defines the wire-level data transfer objects and error
// taxonomy for the local partition-log consumer group coordinator.
//
// The protocol package is intentionally free of behaviour: it only names the
// shapes that cross the HTTP boundary and the stable failure categories that
// independent tests assert on. The computation lives in the kernel package.
package protocol

import "time"

// FailureCode is a stable, machine-readable failure category. Independent
// tests assert on these exact strings; do not renumber or rename them.
type FailureCode string

const (
	// FailureUnknownMember: the request names a member the coordinator has
	// no active record for (never joined, already left, or expired).
	FailureUnknownMember FailureCode = "UNKNOWN_MEMBER"

	// FailureFencedGeneration: the request carries a generation older than
	// the one the named member currently holds. This is the "slow member /
	// late commit" rejection: the commit position is bound to member AND
	// generation, and an old generation is fenced.
	FailureFencedGeneration FailureCode = "ILLEGAL_GENERATION"

	// FailureNotOwner: the member is alive and at the current generation but
	// is not the valid owner of the partition named in the request.
	FailureNotOwner FailureCode = "NOT_OWNER"

	// FailureUnknownPartition: the partition id is outside the configured
	// range [0, partitionCount).
	FailureUnknownPartition FailureCode = "UNKNOWN_PARTITION"

	// FailureUnavailablePartition: the partition is mid-transfer and cannot
	// be committed to or revoked yet. Its sub-reason distinguishes the cases:
	//   PENDING_REVOCATION  - revocation of the previous owner is unconfirmed,
	//                         therefore no new generation may be granted.
	//   NOT_REVOKING        - a revoke-confirm arrived for a partition that is
	//                         not awaiting that member's revocation.
	FailureUnavailablePartition FailureCode = "PARTITION_UNAVAILABLE"

	// FailureAlreadyMember: a join used a member id that is currently active.
	FailureAlreadyMember FailureCode = "ALREADY_MEMBER"

	// FailureInvalidRequest: malformed payload or out-of-range argument.
	FailureInvalidRequest FailureCode = "INVALID_REQUEST"

	// FailureConflict: a serialised command lost an optimistic concurrency
	// race against the group version. The caller should retry.
	FailureConflict FailureCode = "VERSION_CONFLICT"
)

// Sub-reasons carried inside FailureUnavailablePartition details.
const (
	SubReasonPendingRevocation = "PENDING_REVOCATION"
	SubReasonNotRevoking       = "NOT_REVOKING"
)

// APIError is the single error envelope returned for any non-2xx result.
type APIError struct {
	Code      FailureCode `json:"code"`
	Message   string      `json:"message"`
	RequestID string      `json:"request_id"`
	// Details holds structured, item-level failures (per-partition results),
	// so a batch request can report which specific partitions failed and why.
	Details []ItemError `json:"details,omitempty"`
	// Uncertain is true when the coordinator cannot prove the operation's
	// outcome (e.g. a forced revocation after session timeout, where the
	// previous owner's final commit may or may not have been persisted).
	Uncertain bool `json:"uncertain,omitempty"`
}

// ItemError is a per-partition failure inside a batch response.
type ItemError struct {
	Partition int         `json:"partition"`
	Code      FailureCode `json:"code"`
	SubReason string      `json:"sub_reason,omitempty"`
	Message   string      `json:"message"`
}

// Error implements the error interface.
func (e *APIError) Error() string {
	if e == nil {
		return "<nil>"
	}
	s := string(e.Code) + ": " + e.Message
	if e.RequestID != "" {
		s += " (request " + e.RequestID + ")"
	}
	return s
}

// ---- Group configuration ----

// CreateGroupRequest configures a new consumer group.
type CreateGroupRequest struct {
	GroupID        string `json:"group_id"`
	PartitionCount int    `json:"partition_count"`
	// SessionTimeout is how long after the last heartbeat a member may remain
	// before the sweeper force-removes it.
	SessionTimeout string `json:"session_timeout,omitempty"`
}

// ---- Membership ----

type JoinRequest struct {
	MemberID string `json:"member_id"`
}

type JoinResponse struct {
	MemberID   string `json:"member_id"`
	Generation int64  `json:"generation"`
	// Pending is true when the member joins while a rebalance is in flight;
	// the member is fenced at generation 0 until the plan grants it work.
	Pending bool `json:"pending"`
}

type LeaveRequest struct {
	MemberID string `json:"member_id"`
}

type HeartbeatRequest struct {
	MemberID   string `json:"member_id"`
	Generation int64  `json:"generation"`
}

type HeartbeatResponse struct {
	MemberID   string `json:"member_id"`
	Generation int64  `json:"generation"`
	// Rebalancing is true when the member should re-sync: its assignment may
	// have changed or a new generation exists.
	Rebalancing bool `json:"rebalancing"`
}

// ---- Assignment ----

type PartitionAssignment struct {
	Partition  int    `json:"partition"`
	Owner      string `json:"owner"`
	Generation int64  `json:"generation"`
	// Phase is one of: STABLE, REVOKING, PENDING_GRANT, EMPTY.
	Phase string `json:"phase"`
}

type AssignmentResponse struct {
	MemberID   string                `json:"member_id"`
	Generation int64                 `json:"generation"`
	Partitions []PartitionAssignment `json:"partitions"`
	// GroupPhase is STABLE or REBALANCING.
	GroupPhase string `json:"group_phase"`
}

// ---- Revocation ----

type RevokeConfirmRequest struct {
	MemberID   string `json:"member_id"`
	Generation int64  `json:"generation"`
	Partitions []int  `json:"partitions"`
}

type RevokeResult struct {
	Partition int    `json:"partition"`
	OK        bool   `json:"ok"`
	NewOwner  string `json:"new_owner,omitempty"`
}

type RevokeConfirmResponse struct {
	Results    []RevokeResult `json:"results"`
	Generation int64          `json:"generation"`
	// Settled is true when the whole group has returned to STABLE as a result
	// of this batch of confirmations.
	Settled bool `json:"settled"`
}

// ---- Offsets ----

type CommitItem struct {
	Partition int   `json:"partition"`
	Offset    int64 `json:"offset"`
}

type CommitRequest struct {
	MemberID   string       `json:"member_id"`
	Generation int64        `json:"generation"`
	Items      []CommitItem `json:"items"`
}

type CommitResult struct {
	Partition int   `json:"partition"`
	Offset    int64 `json:"offset"`
	OK        bool  `json:"ok"`
}

type CommitResponse struct {
	Results []CommitResult `json:"results"`
}

type FetchOffsetRequest struct {
	MemberID   string `json:"member_id"`
	Generation int64  `json:"generation,omitempty"`
	Partition  int    `json:"partition"`
}

type FetchOffsetResponse struct {
	Partition  int    `json:"partition"`
	Offset     int64  `json:"offset"`
	Owner      string `json:"owner"`
	Generation int64  `json:"generation"`
}

// ---- Introspection / replay ----

type PartitionStateView struct {
	Partition  int    `json:"partition"`
	Owner      string `json:"owner"`
	Generation int64  `json:"generation"`
	Phase      string `json:"phase"`
	// PrevOwner / PrevGeneration track the ownership being revoked while the
	// partition is REVOKING. They are empty otherwise.
	PrevOwner      string `json:"prev_owner,omitempty"`
	PrevGeneration int64  `json:"prev_generation,omitempty"`
	// PendingOwner is the planned new owner once revocation is confirmed.
	PendingOwner string `json:"pending_owner,omitempty"`
	Offset       int64  `json:"offset"`
}

type MemberView struct {
	MemberID   string    `json:"member_id"`
	Generation int64     `json:"generation"`
	JoinedAt   time.Time `json:"joined_at"`
	LastSeen   time.Time `json:"last_seen"`
	Active     bool      `json:"active"`
}

type GroupStateResponse struct {
	GroupID        string               `json:"group_id"`
	Generation     int64                `json:"generation"`
	Phase          string               `json:"phase"`
	Version        int64                `json:"version"`
	PartitionCount int                  `json:"partition_count"`
	Members        []MemberView         `json:"members"`
	Partitions     []PartitionStateView `json:"partitions"`
	// Uncertain lists partition ids whose current value cannot be proven,
	// e.g. force-revoked after the owner timed out before a final commit.
	Uncertain []int `json:"uncertain,omitempty"`
}

type Event struct {
	Seq       int64     `json:"seq"`
	GroupID   string    `json:"group_id"`
	Type      string    `json:"type"`
	Version   int64     `json:"version"`
	Timestamp time.Time `json:"timestamp"`
	RequestID string    `json:"request_id,omitempty"`
	Detail    []byte    `json:"detail,omitempty"`
}

type ReplayResponse struct {
	GroupID string  `json:"group_id"`
	Events  []Event `json:"events"`
}

// TraceEvent is one correlated diagnostic step, returned by the tracer.
type TraceEvent struct {
	Time       time.Time      `json:"time"`
	RequestID  string         `json:"request_id"`
	Method     string         `json:"method"`
	Path       string         `json:"path"`
	MemberID   string         `json:"member_id,omitempty"`
	Generation int64          `json:"generation,omitempty"`
	Step       string         `json:"step"`
	Version    int64          `json:"version,omitempty"`
	Location   string         `json:"location,omitempty"`
	OK         bool           `json:"ok"`
	FailCode   FailureCode    `json:"fail_code,omitempty"`
	FailDetail string         `json:"fail_detail,omitempty"`
	Uncertain  bool           `json:"uncertain,omitempty"`
	Extra      map[string]any `json:"extra,omitempty"`
}
