package kernel

import (
	"fmt"
	"sort"
	"time"
)

// Error is a kernel-level failure carrying the stable protocol category plus
// optional per-item details. The HTTP layer maps it 1:1 to protocol.APIError.
type Error struct {
	Code      string
	Message   string
	Items     []ItemFailure
	Uncertain bool
}

// ItemFailure is a per-partition failure inside a batch command.
type ItemFailure struct {
	Partition int
	Code      string
	SubReason string
	Message   string
}

func (e *Error) Error() string {
	if e == nil {
		return "<nil>"
	}
	return e.Code + ": " + e.Message
}

func kerr(code, msg string) *Error { return &Error{Code: code, Message: msg} }

// CommitItem is a single partition/offset in a commit request.
type CommitItem struct {
	Partition int
	Offset    int64
}

// ---- Create ----

// CreateGroup produces the creation event for a brand new group.
func CreateGroup(partitionCount int, sessionTimeout time.Duration, at time.Time, reqID string) ([]Event, error) {
	if partitionCount <= 0 {
		return nil, kerr("INVALID_REQUEST", "partition_count must be positive")
	}
	if sessionTimeout <= 0 {
		sessionTimeout = defaultSessionTimeout
	}
	return []Event{{
		Type: EvGroupCreated, At: at, RequestID: reqID,
		GroupCount: partitionCount, SessionTimeout: sessionTimeout,
	}}, nil
}

// ---- Join ----

// Join adds a member and immediately runs a rebalance so the newcomer can be
// granted work. Events are built against a clone; the coordinator folds the
// persisted batch to obtain authoritative state.
func Join(s *State, memberID string, at time.Time, reqID string) ([]Event, error) {
	if memberID == "" {
		return nil, kerr("INVALID_REQUEST", "member_id is required")
	}
	if _, active := s.ActiveMember(memberID); active {
		return nil, kerr("ALREADY_MEMBER", fmt.Sprintf("member %q already active", memberID))
	}
	c := s.Clone()
	addMember(c, memberID, at)
	evs := []Event{{Type: EvMemberJoined, At: at, RequestID: reqID, MemberID: memberID}}
	evs = append(evs, runRebalance(c, at, reqID)...)
	return evs, nil
}

// ---- Leave (clean) ----

// Leave explicitly removes a member. A clean leave IS the revocation
// confirmation for everything that member owned: its REVOKING partitions are
// released deterministically (no lost-final-offset uncertainty), then the
// rebalance grants freed partitions in the same batch.
func Leave(s *State, memberID string, at time.Time, reqID string) ([]Event, error) {
	if _, ok := s.ActiveMember(memberID); !ok {
		return nil, kerr("UNKNOWN_MEMBER", fmt.Sprintf("member %q is not active", memberID))
	}
	c := s.Clone()
	releases := revokeDeparting(c, memberID, false)
	dropMember(c, memberID)

	var evs []Event
	if len(releases) > 0 {
		evs = append(evs, Event{Type: EvPartitionRevoked, At: at, RequestID: reqID, Generation: c.Generation, Revoke: releases})
	}
	evs = append(evs, Event{Type: EvMemberLeft, At: at, RequestID: reqID, MemberID: memberID})
	evs = append(evs, runRebalance(c, at, reqID)...)
	return evs, nil
}

// ---- Expire (forced) ----

// Expire force-removes a member whose session has timed out. Partitions it
// still owned are force-revoked and marked uncertain: the previous owner may
// never have committed its final offset, so the resulting position is a failure
// the coordinator reports rather than hides.
func Expire(s *State, memberID string, at time.Time, reqID string) ([]Event, error) {
	m, ok := s.ActiveMember(memberID)
	if !ok {
		return nil, kerr("UNKNOWN_MEMBER", fmt.Sprintf("member %q is not active", memberID))
	}
	_ = m
	c := s.Clone()
	releases := revokeDeparting(c, memberID, true)
	dropMember(c, memberID)

	var evs []Event
	if len(releases) > 0 {
		evs = append(evs, Event{Type: EvPartitionRevoked, At: at, RequestID: reqID, Force: true, Generation: c.Generation, Revoke: releases})
	}
	evs = append(evs, Event{Type: EvMemberExpired, At: at, RequestID: reqID, MemberID: memberID, Force: true})
	evs = append(evs, runRebalance(c, at, reqID)...)
	return evs, nil
}

// ---- Heartbeat ----

// Heartbeat records liveness and rejects a stale generation so a slow member
// cannot keep itself alive with fenced credentials.
func Heartbeat(s *State, memberID string, gen int64, at time.Time, reqID string) ([]Event, bool, error) {
	m, ok := s.ActiveMember(memberID)
	if !ok {
		return nil, false, kerr("UNKNOWN_MEMBER", fmt.Sprintf("member %q is not active", memberID))
	}
	if gen != m.Generation {
		return nil, false, &Error{Code: "ILLEGAL_GENERATION",
			Message: fmt.Sprintf("member %q heartbeat generation %d != current %d", memberID, gen, m.Generation)}
	}
	rebalancing := s.Phase == PhaseRebalancing
	return []Event{{Type: EvHeartbeat, At: at, RequestID: reqID, MemberID: memberID, Generation: gen}}, rebalancing, nil
}

// ---- Revocation confirmation ----

// ConfirmRevocation validates each item independently. Valid confirmations
// withdraw old ownership (REVOKING -> PENDING_GRANT, no valid owner) and, in the
// same batch, grant the planned new owner whenever one exists. Unconfirmed
// partitions are never granted, so old and new ownership cannot overlap.
func ConfirmRevocation(s *State, memberID string, gen int64, parts []int, at time.Time, reqID string) ([]Event, bool, *Error) {
	m, ok := s.ActiveMember(memberID)
	if !ok {
		return nil, false, kerr("UNKNOWN_MEMBER", fmt.Sprintf("member %q is not active", memberID))
	}
	if gen != m.Generation {
		return nil, false, &Error{Code: "ILLEGAL_GENERATION",
			Message: fmt.Sprintf("member %q generation %d != current %d", memberID, gen, m.Generation)}
	}
	c := s.Clone()
	var revokes []RevokeMove
	var failures []ItemFailure
	seen := map[int]bool{}
	for _, pid := range parts {
		if seen[pid] {
			continue
		}
		seen[pid] = true
		if pid < 0 || pid >= c.PartitionCount {
			failures = append(failures, ItemFailure{Partition: pid, Code: "UNKNOWN_PARTITION", Message: "out of range"})
			continue
		}
		p := c.Partitions[pid]
		switch {
		case p.Phase != PRevoking:
			failures = append(failures, ItemFailure{Partition: pid, Code: "PARTITION_UNAVAILABLE", SubReason: "NOT_REVOKING",
				Message: fmt.Sprintf("partition %d is not awaiting revocation (phase=%s)", pid, p.Phase)})
		case p.PrevOwner != memberID:
			failures = append(failures, ItemFailure{Partition: pid, Code: "NOT_OWNER",
				Message: fmt.Sprintf("partition %d is being revoked from %q, not %q", pid, p.PrevOwner, memberID)})
		case p.PrevGeneration != gen:
			failures = append(failures, ItemFailure{Partition: pid, Code: "ILLEGAL_GENERATION",
				Message: fmt.Sprintf("partition %d revocation expected generation %d, got %d", pid, p.PrevGeneration, gen)})
		default:
			revokes = append(revokes, RevokeMove{Partition: pid, OldOwner: memberID, NewOwner: p.PendingOwner, OldGen: gen, NewGen: c.Generation})
		}
	}
	var evs []Event
	if len(revokes) > 0 {
		evs = append(evs, Event{Type: EvPartitionRevoked, At: at, RequestID: reqID, Generation: c.Generation, Revoke: revokes})
		for _, r := range revokes {
			revoke(c, r.Partition, r.OldOwner, r.OldGen, r.NewOwner, false)
		}
		// Grant every released partition whose planned owner is still active.
		var grants []GrantMove
		for _, p := range c.Partitions {
			if p.Phase == PPendingGrant && p.PendingOwner != "" && isActive(c, p.PendingOwner) {
				grants = append(grants, GrantMove{Partition: p.ID, NewOwner: p.PendingOwner, NewGen: c.Generation})
			}
		}
		if len(grants) > 0 {
			evs = append(evs, Event{Type: EvPartitionGranted, At: at, RequestID: reqID, Generation: c.Generation, Grant: grants})
			for _, g := range grants {
				grant(c, g.Partition, g.NewOwner, g.NewGen)
			}
		}
		deriveMemberGens(c)
	}
	settled := groupSettled(c)
	if settled {
		c.Phase = PhaseStable
	}
	var err *Error
	if len(failures) > 0 {
		err = &Error{Code: "PARTIAL_FAILURE", Message: "some partitions were not confirmed", Items: failures}
	}
	return evs, settled, err
}

// ---- Offset commit ----

// Commit validates each item against the ownership invariant. Accepted items
// become OFFSET_COMMITTED events; rejected items carry a specific category:
//
//	UNKNOWN_MEMBER          - member gone/unknown
//	ILLEGAL_GENERATION      - late commit from a fenced (slow) member
//	NOT_OWNER               - member is not the valid owner
//	PARTITION_UNAVAILABLE   - mid-transfer; revocation unconfirmed or pre-grant
//	UNKNOWN_PARTITION       - id out of range
func Commit(s *State, memberID string, gen int64, items []CommitItem, at time.Time, reqID string) ([]Event, []ItemFailure) {
	m, ok := s.ActiveMember(memberID)
	if !ok {
		// category per item so callers see the exact failure
		f := make([]ItemFailure, 0, len(items))
		for _, it := range items {
			f = append(f, ItemFailure{Partition: it.Partition, Code: "UNKNOWN_MEMBER", Message: "member not active"})
		}
		return nil, f
	}
	if gen != m.Generation {
		f := make([]ItemFailure, 0, len(items))
		for _, it := range items {
			f = append(f, ItemFailure{Partition: it.Partition, Code: "ILLEGAL_GENERATION",
				Message: fmt.Sprintf("member %q commit generation %d != current %d", memberID, gen, m.Generation)})
		}
		return nil, f
	}
	// de-duplicate within a batch: last value wins, processed in pid order
	last := map[int]int64{}
	for _, it := range items {
		last[it.Partition] = it.Offset
	}
	pids := make([]int, 0, len(last))
	for pid := range last {
		pids = append(pids, pid)
	}
	sort.Ints(pids)

	var evs []Event
	var failures []ItemFailure
	for _, pid := range pids {
		off := last[pid]
		if pid < 0 || pid >= s.PartitionCount {
			failures = append(failures, ItemFailure{Partition: pid, Code: "UNKNOWN_PARTITION", Message: "out of range"})
			continue
		}
		p := s.Partitions[pid]
		switch {
		case p.Phase == PRevoking:
			if p.PrevOwner == memberID && p.PrevGeneration == gen {
				failures = append(failures, ItemFailure{Partition: pid, Code: "PARTITION_UNAVAILABLE", SubReason: "PENDING_REVOCATION",
					Message: "partition revocation unconfirmed; commit frozen until transfer settles"})
			} else {
				failures = append(failures, ItemFailure{Partition: pid, Code: "NOT_OWNER", Message: "partition has no valid owner for this member"})
			}
		case p.Phase == PPendingGrant:
			failures = append(failures, ItemFailure{Partition: pid, Code: "PARTITION_UNAVAILABLE", SubReason: "PENDING_REVOCATION",
				Message: "old ownership withdrawn, new generation not yet granted"})
		case p.Owner != memberID:
			failures = append(failures, ItemFailure{Partition: pid, Code: "NOT_OWNER", Message: fmt.Sprintf("owned by %q", p.Owner)})
		case p.Generation != gen:
			failures = append(failures, ItemFailure{Partition: pid, Code: "ILLEGAL_GENERATION", Message: "stale owner generation"})
		default:
			evs = append(evs, Event{Type: EvOffsetCommitted, At: at, RequestID: reqID, MemberID: memberID, Generation: gen,
				CommitPartition: pid, CommitOffset: off, CommitOwner: memberID, CommitGen: gen})
		}
	}
	return evs, failures
}
