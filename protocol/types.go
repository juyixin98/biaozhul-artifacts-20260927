package protocol

import "time"

// Phase is the lifecycle phase of a generation.
type Phase string

const (
	// PhaseEmpty is the phase of generation 0 before the first activation.
	PhaseEmpty Phase = "EMPTY"
	// PhaseStable means the current generation is active and all assigned
	// partitions have an effective owner.
	PhaseStable Phase = "STABLE"
	// PhaseRevoking means revocations from the previous generation are in
	// flight; the new generation has been computed but not yet granted.
	PhaseRevoking Phase = "REVOKING"
	// PhaseBlocked means the planned generation cannot activate because at
	// least one partition is QUARANTINED. The group stays in the previous
	// generation until the partition is recovered.
	PhaseBlocked Phase = "BLOCKED"
)

// PartitionState is the state of a single partition inside a generation
// transition.
type PartitionState string

const (
	// PSOwned: the partition has an effective owner in the active
	// generation.
	PSOwned PartitionState = "OWNED"
	// PSRevoking: revocation has been issued to the old owner; the
	// partition cannot be granted to the new owner yet.
	PSRevoking PartitionState = "REVOKING"
	// PSRevoked: the old owner released the partition; it may be granted.
	PSRevoked PartitionState = "REVOKED"
	// PSQuarantined: revocation was not confirmed within the timeout. The
	// partition is frozen: it has no effective owner and cannot be granted
	// until an out-of-band recovery confirmation arrives.
	PSQuarantined PartitionState = "QUARANTINED"
	// PSGranted: the partition was assigned in the activated generation and
	// the new owner has been handed ownership.
	PSGranted PartitionState = "GRANTED"
	// PSUnassigned: the partition belongs to no member in the active
	// generation (no member subscribes to its topic).
	PSUnassigned PartitionState = "UNASSIGNED"
)

// MemberState is the lifecycle state of a member within the group.
type MemberState string

const (
	// MSPending: joined but not yet part of an activated generation.
	MSPending MemberState = "PENDING"
	// MSStable: member of the active generation.
	MSStable MemberState = "STABLE"
	// MSRevoking: the member has outstanding revocations it must release.
	MSRevoking MemberState = "REVOKING"
	// MSLeaving: orderly leave in progress; no new ownership may be granted.
	MSLeaving MemberState = "LEAVING"
	// MSExpired: heartbeat deadline missed; pending revocations keep it
	// around until they resolve.
	MSExpired MemberState = "EXPIRED"
)

// Subscription is the set of topics a member consumes.
type Subscription struct {
	Topics []Topic `json:"topics"`
}

func (s Subscription) topicSet() map[Topic]struct{} {
	m := make(map[Topic]struct{}, len(s.Topics))
	for _, t := range s.Topics {
		m[t] = struct{}{}
	}
	return m
}

// Subscribes reports whether the member subscribes to topic t.
func (s Subscription) Subscribes(t Topic) bool {
	_, ok := s.topicSet()[t]
	return ok
}

// MemberSpec describes a group member.
type MemberSpec struct {
	ID           MemberID     `json:"id"`
	Subscription Subscription `json:"subscription"`
	// SessionTimeout is how long without a heartbeat before the member is
	// expired. Zero means the group default applies.
	SessionTimeout time.Duration `json:"session_timeout_ms,omitempty"`
	// Metadata is opaque member metadata carried in join requests.
	Metadata string `json:"metadata,omitempty"`
}

// Owner is the effective ownership record of a partition.
type Owner struct {
	Member     MemberID   `json:"member"`
	Generation Generation `json:"generation"`
}

// PendingRevoke records a revocation that the old owner must confirm.
type PendingRevoke struct {
	TP         TP
	OldOwner   MemberID
	NewOwner   MemberID // empty when the partition becomes unassigned
	Generation Generation
	Deadline   time.Time
	// QuarantinedAt is zero until the revocation times out.
	QuarantinedAt time.Time
}

// RevokeKey identifies a pending revocation by partition and generation.
type RevokeKey struct {
	TP         TP
	Generation Generation
}

// OffsetRecord is one committed consumer offset.
type OffsetRecord struct {
	TP         TP
	Offset     int64
	LeaderEpoch int64 // -1 when unknown; carried for compatibility, never fenced on
	Member     MemberID
	Generation Generation
	CommittedAt time.Time
}

// TopicSpec declares a topic and its fixed partition count.
type TopicSpec struct {
	Name       Topic `json:"name"`
	Partitions int   `json:"partitions"`
}

// GroupConfig holds the tunable timeouts for a group.
type GroupConfig struct {
	// RevokeTimeout bounds how long a member gets to confirm a revocation.
	RevokeTimeout time.Duration
	// QuarantineTimeout bounds how long a timed-out revocation stays
	// quarantined before the sweep force-releases the partition. It is a
	// local safety valve; setting it very large leaves release entirely to
	// out-of-band recovery confirmations.
	QuarantineTimeout time.Duration
	// DefaultSessionTimeout applies to members that join without one.
	DefaultSessionTimeout time.Duration
}

// DefaultGroupConfig returns conservative demo-friendly timeouts.
func DefaultGroupConfig() GroupConfig {
	return GroupConfig{
		RevokeTimeout:         10 * time.Second,
		QuarantineTimeout:     30 * time.Second,
		DefaultSessionTimeout: 15 * time.Second,
	}
}

// GroupState is the complete, replay-derived state of one group. It is the
// single source of truth persisted by the storage module and served by the
// state endpoint.
type GroupState struct {
	Name            string
	Generation      Generation
	Phase           Phase
	Config          GroupConfig
	Topics          []TopicSpec
	Members         map[MemberID]*MemberRuntime
	Owners          map[TP]Owner
	PartitionStates map[TP]PartitionState
	// PendingByTP indexes open revocations by partition.
	PendingByTP map[TP]*PendingRevoke
	Offsets     map[TP]OffsetRecord
	// Planned* carry the not-yet-activated generation prepared by the last
	// PREPARE_REBALANCE event. They persist across requests and restarts so a
	// partition freed late (or force-freed by the sweep) can still be granted
	// within the planned generation.
	PlannedGeneration Generation
	Planned           []PlanEntry
	PlannedUnassigned []TP
}

// MemberRuntime is the mutable runtime state of a member.
type MemberRuntime struct {
	Spec           MemberSpec
	State          MemberState
	LastHeartbeat  time.Time
	JoinedAt       time.Time
	// Owned is the set of partitions the member effectively owns in the
	// active generation.
	Owned map[TP]struct{}
	// Revoking is the set of partitions the member must release.
	Revoking map[TP]struct{}
}

// Clone returns a deep copy of the group state, safe to hand to callers while
// the coordinator keeps mutating the original.
func (s *GroupState) Clone() *GroupState {
	if s == nil {
		return nil
	}
	c := &GroupState{
		Name:       s.Name,
		Generation: s.Generation,
		Phase:      s.Phase,
		Config:     s.Config,
	}
	c.Topics = append([]TopicSpec(nil), s.Topics...)
	c.Members = make(map[MemberID]*MemberRuntime, len(s.Members))
	for id, m := range s.Members {
		mm := &MemberRuntime{
			Spec:          m.Spec,
			State:         m.State,
			LastHeartbeat: m.LastHeartbeat,
			JoinedAt:      m.JoinedAt,
		}
		mm.Owned = cloneTPSet(m.Owned)
		mm.Revoking = cloneTPSet(m.Revoking)
		c.Members[id] = mm
	}
	c.Owners = make(map[TP]Owner, len(s.Owners))
	for tp, o := range s.Owners {
		c.Owners[tp] = o
	}
	c.PartitionStates = make(map[TP]PartitionState, len(s.PartitionStates))
	for tp, ps := range s.PartitionStates {
		c.PartitionStates[tp] = ps
	}
	c.PendingByTP = make(map[TP]*PendingRevoke, len(s.PendingByTP))
	for tp, p := range s.PendingByTP {
		pp := *p
		c.PendingByTP[tp] = &pp
	}
	c.Offsets = make(map[TP]OffsetRecord, len(s.Offsets))
	for tp, o := range s.Offsets {
		c.Offsets[tp] = o
	}
	c.PlannedGeneration = s.PlannedGeneration
	c.Planned = append([]PlanEntry(nil), s.Planned...)
	c.PlannedUnassigned = append([]TP(nil), s.PlannedUnassigned...)
	return c
}

func cloneTPSet(in map[TP]struct{}) map[TP]struct{} {
	if in == nil {
		return nil
	}
	out := make(map[TP]struct{}, len(in))
	for tp := range in {
		out[tp] = struct{}{}
	}
	return out
}
