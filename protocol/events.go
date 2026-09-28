package protocol

import (
	"encoding/json"
	"time"
)

// EventType enumerates journal event types.
type EventType string

const (
	EvGroupCreated        EventType = "GROUP_CREATED"
	EvMemberJoined        EventType = "MEMBER_JOINED"
	EvMemberLeft          EventType = "MEMBER_LEFT"
	EvMemberExpired       EventType = "MEMBER_EXPIRED"
	EvHeartbeat           EventType = "HEARTBEAT"
	EvPrepareRebalance    EventType = "PREPARE_REBALANCE"
	EvRevokeIssued        EventType = "REVOKE_ISSUED"
	EvRevokeAcked         EventType = "REVOKE_ACKED"
	EvRevokeQuarantined   EventType = "REVOKE_QUARANTINED"
	EvPartitionForceFreed EventType = "PARTITION_FORCE_FREED"
	EvGenerationActivated EventType = "GENERATION_ACTIVATED"
	EvOffsetCommitted     EventType = "OFFSET_COMMITTED"
)

// Event is one append-only journal record. Detail is one of the *Detail
// structs below; it round-trips through JSON via the registered type table in
// registry.go.
type Event struct {
	Seq       Seq           `json:"seq"`
	Group     string        `json:"group"`
	Type      EventType     `json:"type"`
	At        time.Time     `json:"at"`
	RequestID string        `json:"request_id"`
	Detail    any           `json:"detail,omitempty"`
	RawDetail *json.RawMessage `json:"-"`
}

// GroupCreatedDetail creates the group with its topic universe and config.
type GroupCreatedDetail struct {
	Config GroupConfig `json:"config"`
	Topics []TopicSpec `json:"topics"`
}

// MemberJoinedDetail records a new member.
type MemberJoinedDetail struct {
	Member MemberSpec `json:"member"`
	At     time.Time  `json:"at"`
}

// MemberLeftDetail records an orderly leave.
type MemberLeftDetail struct {
	Member MemberID `json:"member"`
	At     time.Time `json:"at"`
}

// MemberExpiredDetail records a heartbeat timeout.
type MemberExpiredDetail struct {
	Member MemberID  `json:"member"`
	At     time.Time `json:"at"`
}

// HeartbeatDetail refreshes a member's liveness.
type HeartbeatDetail struct {
	Member MemberID  `json:"member"`
	At     time.Time `json:"at"`
}

// PlanEntry is one partition's transition inside a prepared rebalance.
type PlanEntry struct {
	TP        TP        `json:"tp"`
	OldOwner  MemberID  `json:"old_owner"`
	NewOwner  MemberID  `json:"new_owner"`
	Retained  bool      `json:"retained"`
}

// PrepareRebalanceDetail announces a new planned generation and the full
// per-partition plan (retained, revoke-then-grant, or free). The plan is
// recorded *before* any revocation is issued; a partition never appears
// granted here.
type PrepareRebalanceDetail struct {
	Generation Generation  `json:"generation"`
	Reason     string      `json:"reason"`
	Plan       []PlanEntry `json:"plan"`
}

// RevokeIssuedDetail records one revocation handed to an old owner.
type RevokeIssuedDetail struct {
	TP         TP         `json:"tp"`
	OldOwner   MemberID   `json:"old_owner"`
	NewOwner   MemberID   `json:"new_owner"`
	Generation Generation `json:"generation"`
	Deadline   time.Time  `json:"deadline"`
}

// RevokeAckedDetail records the old owner releasing a partition.
type RevokeAckedDetail struct {
	TP         TP         `json:"tp"`
	OldOwner   MemberID   `json:"old_owner"`
	Generation Generation `json:"generation"`
	At         time.Time  `json:"at"`
}

// RevokeQuarantinedDetail records a revocation whose deadline elapsed.
type RevokeQuarantinedDetail struct {
	TP         TP         `json:"tp"`
	OldOwner   MemberID   `json:"old_owner"`
	Generation Generation `json:"generation"`
	At         time.Time  `json:"at"`
}

// PartitionForceFreedDetail records the sweep releasing a long-quarantined
// partition so a blocked rebalance can proceed. This is the only path that
// overrides a missing revocation confirmation; the uncertainty is recorded
// explicitly in the journal and the state endpoint.
type PartitionForceFreedDetail struct {
	TP         TP         `json:"tp"`
	OldOwner   MemberID   `json:"old_owner"`
	Generation Generation `json:"generation"`
	Reason     string     `json:"reason"`
	At         time.Time  `json:"at"`
}

// GenerationActivatedDetail marks a planned generation effective and lists
// every grant (including retained partitions, so membership of a generation
// is fully reconstructable from the journal).
type GenerationActivatedDetail struct {
	Generation Generation  `json:"generation"`
	Grants     []PlanEntry `json:"grants"`
	Unassigned []TP        `json:"unassigned"`
}

// OffsetCommittedDetail records one accepted offset commit, bound to the
// member and generation that owned the partition.
type OffsetCommittedDetail struct {
	TP          TP         `json:"tp"`
	Offset      int64      `json:"offset"`
	LeaderEpoch int64      `json:"leader_epoch"`
	Member      MemberID   `json:"member"`
	Generation  Generation `json:"generation"`
	At          time.Time  `json:"at"`
}
