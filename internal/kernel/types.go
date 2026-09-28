// Package kernel is the pure, deterministic computation core of the
// coordinator. It owns:
//
//   - the in-memory State of a group (membership + per-partition ownership),
//   - the sticky partition planner that preserves unchanged ownership,
//   - the state-machine transition from commands to Events,
//   - a fold (Reducer) that rebuilds State purely from an event log.
//
// The kernel performs no I/O, holds no locks, and never reads the wall clock:
// every command takes the current State and a timestamp and returns the events
// that must be appended. This makes every transition independently testable and
// makes restart recovery a pure fold over persisted events.
package kernel

import "time"

// GroupPhase is the lifecycle phase of a whole group.
type GroupPhase string

const (
	PhaseStable      GroupPhase = "STABLE"
	PhaseRebalancing GroupPhase = "REBALANCING"
)

// PartitionPhase is the per-partition transfer state machine.
//
//	STABLE       - owner holds the partition at the current generation
//	REVOKING     - old owner still owns; revocation demanded, not confirmed
//	PENDING_GRANT- revocation confirmed; no valid owner until the planned
//	               new owner is granted (old and new owner never co-exist)
//	EMPTY        - never assigned / group has no members
type PartitionPhase string

const (
	PStable       PartitionPhase = "STABLE"
	PRevoking     PartitionPhase = "REVOKING"
	PPendingGrant PartitionPhase = "PENDING_GRANT"
	PEmpty        PartitionPhase = "EMPTY"
)

// Member is a consumer group member.
type Member struct {
	ID         string
	Generation int64 // generation this member is currently fenced at (0 = pending/unknown)
	JoinedAt   time.Time
	LastSeen   time.Time
	Active     bool
}

// Partition is one unit of ownership with its full transfer bookkeeping.
type Partition struct {
	ID    int
	Phase PartitionPhase

	// Currently valid ownership (the unique owner invariant is expressed on
	// these two fields). When Phase is PENDING_GRANT they are zeroed.
	Owner      string
	Generation int64

	// Ownership being revoked while REVOKING.
	PrevOwner      string
	PrevGeneration int64

	// Planned next owner once the revocation is confirmed.
	PendingOwner string

	// Last committed offset and its provenance.
	Offset      int64
	OffsetOwner string
	OffsetGen   int64

	// Uncertain is set when the partition was force-revoked after an owner
	// session timeout: the previous owner's final offset is unknowable.
	Uncertain bool
}

// State is the full materialised state of one group. It is rebuilt purely by
// folding events; the store never mutates it directly.
type State struct {
	GroupID        string
	PartitionCount int
	Generation     int64 // current / in-flight generation
	Phase          GroupPhase
	Version        int64 // monotonic event count, also used for optimistic cc
	SessionTimeout time.Duration

	Members    map[string]*Member
	Partitions []*Partition
}

// NewState creates an empty group state.
func NewState(groupID string, partitionCount int, sessionTimeout time.Duration) *State {
	s := &State{
		GroupID:        groupID,
		PartitionCount: partitionCount,
		Generation:     0,
		Phase:          PhaseStable,
		SessionTimeout: sessionTimeout,
		Members:        map[string]*Member{},
		Partitions:     make([]*Partition, partitionCount),
	}
	for i := 0; i < partitionCount; i++ {
		s.Partitions[i] = &Partition{ID: i, Phase: PEmpty}
	}
	return s
}

// ActiveMembers returns the sorted active member ids.
func (s *State) ActiveMembers() []string {
	out := make([]string, 0, len(s.Members))
	for id, m := range s.Members {
		if m.Active {
			out = append(out, id)
		}
	}
	sortStrings(out)
	return out
}

// ActiveMember returns the member record and presence.
func (s *State) ActiveMember(id string) (*Member, bool) {
	m, ok := s.Members[id]
	return m, ok && m.Active
}

// Clone returns a deep copy of State used while building a command's event
// batch. The clone is scratch space; authoritative state comes from folding the
// persisted events.
func (s *State) Clone() *State {
	c := &State{
		GroupID:        s.GroupID,
		PartitionCount: s.PartitionCount,
		Generation:     s.Generation,
		Phase:          s.Phase,
		Version:        s.Version,
		SessionTimeout: s.SessionTimeout,
		Members:        make(map[string]*Member, len(s.Members)),
		Partitions:     make([]*Partition, len(s.Partitions)),
	}
	for id, m := range s.Members {
		mc := *m
		c.Members[id] = &mc
	}
	for i, p := range s.Partitions {
		pc := *p
		c.Partitions[i] = &pc
	}
	return c
}

// ---- Events ----
//
// Every state change is one of these events. Detail JSON is produced by the
// store layer; the kernel passes typed payloads. Event is the envelope.

type EventType string

const (
	EvGroupCreated       EventType = "GROUP_CREATED"
	EvMemberJoined       EventType = "MEMBER_JOINED"
	EvMemberLeft         EventType = "MEMBER_LEFT"
	EvMemberExpired      EventType = "MEMBER_EXPIRED"
	EvHeartbeat          EventType = "HEARTBEAT"
	EvRebalanceStarted   EventType = "REBALANCE_STARTED"
	EvRevocationDemanded EventType = "REVOCATION_DEMANDED"
	EvPartitionRevoked   EventType = "PARTITION_REVOKED"
	EvPartitionGranted   EventType = "PARTITION_GRANTED"
	EvOffsetCommitted    EventType = "OFFSET_COMMITTED"
)

// Event is a single state transition record.
type Event struct {
	Type      EventType
	Version   int64     // version AFTER applying this event (1-based)
	At        time.Time // authoritative time of the transition
	RequestID string

	// Only the field relevant to Type is set.
	GroupCount     int
	SessionTimeout time.Duration

	MemberID   string
	Generation int64

	Plan     []PlanMove // REBALANCE_STARTED
	Demanded []PlanMove // REVOCATION_DEMANDED (STABLE -> REVOKING)
	Revoke   []RevokeMove
	Grant    []GrantMove

	CommitPartition int
	CommitOffset    int64
	CommitOwner     string
	CommitGen       int64

	Force bool // MEMBER_EXPIRED forced revocation
}

// PlanMove is one decision made by the planner at a rebalance.
//
//   - Retain: the partition keeps its owner, whose generation advances to NewGen.
//   - Move:   the partition transfers Old -> New. It must be revoked by Old and
//     confirmed before New is granted.
type PlanMove struct {
	Partition int
	Action    string // "RETAIN" or "MOVE"
	OldOwner  string
	NewOwner  string
	OldGen    int64
	NewGen    int64
}

// RevokeMove records a confirmed revocation (old owner released the partition).
type RevokeMove struct {
	Partition int
	OldOwner  string
	NewOwner  string // planned grantee, may be empty
	OldGen    int64
	NewGen    int64
}

// GrantMove records a new owner taking a released partition.
type GrantMove struct {
	Partition int
	NewOwner  string
	NewGen    int64
}
