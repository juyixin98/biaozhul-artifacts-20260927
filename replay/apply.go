// Package replay is the single state-mutation engine of the coordinator.
// GroupState changes only by applying journal events; both the live
// coordinator path (events appended inside the storage transaction) and the
// restart path (events read back from storage) call Apply, so the two cannot
// diverge. The package deliberately imports neither the coordinator nor a
// storage implementation: its only inputs are events.
package replay

import (
	"fmt"

	"example.com/cgcoord/protocol"
)

// EventReader is the minimal storage surface replay needs.
type EventReader interface {
	// LoadGroup returns the persisted state, or (nil,nil) when the group
	// does not exist. The blob is used only as an optimization; replay
	// ignores it and rebuilds from events.
	LoadGroup(name string) (*protocol.GroupState, error)
	ReadEvents(group string, from protocol.Seq, limit int) ([]protocol.Event, error)
}

// NewEmptyState builds the zero value shape callers expect before the
// GROUP_CREATED event.
func NewEmptyState(name string) *protocol.GroupState {
	return &protocol.GroupState{
		Name:            name,
		Generation:      0,
		Phase:           protocol.PhaseEmpty,
		Members:         map[protocol.MemberID]*protocol.MemberRuntime{},
		Owners:          map[protocol.TP]protocol.Owner{},
		PartitionStates: map[protocol.TP]protocol.PartitionState{},
		PendingByTP:     map[protocol.TP]*protocol.PendingRevoke{},
		Offsets:         map[protocol.TP]protocol.OffsetRecord{},
	}
}

// Apply folds one event into the state. It returns an error for
// contradictions (unknown partition, wrong generation, duplicate seq);
// duplicate revoke acks are tolerated because the live path and replay must
// agree on events that were actually appended once.
func Apply(s *protocol.GroupState, e protocol.Event) error {
	if e.Group != s.Name {
		return fmt.Errorf("replay: event group %q != state group %q", e.Group, s.Name)
	}
	switch d := e.Detail.(type) {
	case *protocol.GroupCreatedDetail:
		return applyGroupCreated(s, e, d)
	case *protocol.MemberJoinedDetail:
		return applyMemberJoined(s, e, d)
	case *protocol.MemberLeftDetail:
		return applyMemberLeft(s, e, d)
	case *protocol.MemberExpiredDetail:
		return applyMemberExpired(s, e, d)
	case *protocol.HeartbeatDetail:
		return applyHeartbeat(s, e, d)
	case *protocol.PrepareRebalanceDetail:
		return applyPrepare(s, e, d)
	case *protocol.RevokeIssuedDetail:
		return applyRevokeIssued(s, e, d)
	case *protocol.RevokeAckedDetail:
		return applyRevokeAcked(s, e, d)
	case *protocol.RevokeQuarantinedDetail:
		return applyQuarantined(s, e, d)
	case *protocol.PartitionForceFreedDetail:
		return applyForceFreed(s, e, d)
	case *protocol.GenerationActivatedDetail:
		return applyActivated(s, e, d)
	case *protocol.OffsetCommittedDetail:
		return applyOffsetCommitted(s, e, d)
	default:
		return fmt.Errorf("replay: event %d: unsupported detail type %T", e.Seq, e.Detail)
	}
}

func applyGroupCreated(s *protocol.GroupState, e protocol.Event, d *protocol.GroupCreatedDetail) error {
	if s.Phase != protocol.PhaseEmpty {
		return fmt.Errorf("replay: event %d: group %q already created", e.Seq, s.Name)
	}
	s.Config = d.Config
	s.Topics = append([]protocol.TopicSpec(nil), d.Topics...)
	for _, t := range s.Topics {
		for i := 0; i < t.Partitions; i++ {
			tp := protocol.TP{Topic: t.Name, Partition: protocol.Partition(i)}
			s.PartitionStates[tp] = protocol.PSUnassigned
		}
	}
	return nil
}

func applyMemberJoined(s *protocol.GroupState, e protocol.Event, d *protocol.MemberJoinedDetail) error {
	if _, exists := s.Members[d.Member.ID]; exists {
		return fmt.Errorf("replay: event %d: duplicate join for %q", e.Seq, d.Member.ID)
	}
	timeout := d.Member.SessionTimeout
	if timeout == 0 {
		timeout = s.Config.DefaultSessionTimeout
	}
	spec := d.Member
	spec.SessionTimeout = timeout
	s.Members[d.Member.ID] = &protocol.MemberRuntime{
		Spec:          spec,
		State:         protocol.MSPending,
		LastHeartbeat: d.At,
		JoinedAt:      d.At,
		Owned:         map[protocol.TP]struct{}{},
		Revoking:      map[protocol.TP]struct{}{},
	}
	return nil
}

func applyMemberLeft(s *protocol.GroupState, e protocol.Event, d *protocol.MemberLeftDetail) error {
	m := s.Members[d.Member]
	if m == nil {
		return fmt.Errorf("replay: event %d: leave for unknown member %q", e.Seq, d.Member)
	}
	// An orderly leave acks its own revocations first; the member must own
	// nothing by the time MEMBER_LEFT is applied.
	for tp := range m.Owned {
		return fmt.Errorf("replay: event %d: leaving member %q still owns %s", e.Seq, d.Member, tp)
	}
	delete(s.Members, d.Member)
	return nil
}

func applyMemberExpired(s *protocol.GroupState, e protocol.Event, d *protocol.MemberExpiredDetail) error {
	m := s.Members[d.Member]
	if m == nil {
		return fmt.Errorf("replay: event %d: expire unknown member %q", e.Seq, d.Member)
	}
	m.State = protocol.MSExpired
	m.LastHeartbeat = d.At
	return nil
}

func applyHeartbeat(s *protocol.GroupState, e protocol.Event, d *protocol.HeartbeatDetail) error {
	m := s.Members[d.Member]
	if m == nil {
		return fmt.Errorf("replay: event %d: heartbeat from unknown member %q", e.Seq, d.Member)
	}
	m.LastHeartbeat = d.At
	if m.State == protocol.MSExpired {
		// The member reached us again: its revocations (if any) still stand,
		// but it is a live member for accounting purposes.
		if len(m.Revoking) > 0 {
			m.State = protocol.MSRevoking
		} else {
			m.State = protocol.MSStable
		}
	}
	return nil
}

func applyPrepare(s *protocol.GroupState, e protocol.Event, d *protocol.PrepareRebalanceDetail) error {
	if d.Generation != s.Generation+1 {
		return fmt.Errorf("replay: event %d: prepared gen %d, expected %d", e.Seq, d.Generation, s.Generation+1)
	}
	if s.Phase == protocol.PhaseRevoking || s.Phase == protocol.PhaseBlocked {
		return fmt.Errorf("replay: event %d: prepare while gen %d not active", e.Seq, d.Generation)
	}
	if err := assertAllPartitionsKnown(s, e, planTPs(d.Plan)); err != nil {
		return err
	}
	s.Phase = protocol.PhaseRevoking
	s.PlannedGeneration = d.Generation
	s.Planned = append([]protocol.PlanEntry(nil), d.Plan...)
	s.PlannedUnassigned = nil
	for _, pe := range d.Plan {
		if pe.Retained {
			continue
		}
		if pe.OldOwner != "" {
			m := s.Members[pe.OldOwner]
			if m == nil {
				return fmt.Errorf("replay: event %d: revoke from unknown member %q", e.Seq, pe.OldOwner)
			}
			if m.State == protocol.MSStable || m.State == protocol.MSPending {
				m.State = protocol.MSRevoking
			}
		}
	}
	return nil
}

func applyRevokeIssued(s *protocol.GroupState, e protocol.Event, d *protocol.RevokeIssuedDetail) error {
	if d.Generation != s.PlannedGeneration {
		return fmt.Errorf("replay: event %d: revoke for gen %d, planned %d", e.Seq, d.Generation, s.PlannedGeneration)
	}
	if s.Phase != protocol.PhaseRevoking && s.Phase != protocol.PhaseBlocked {
		return fmt.Errorf("replay: event %d: revoke issued in phase %s", e.Seq, s.Phase)
	}
	if !knownTP(s, d.TP) {
		return fmt.Errorf("replay: event %d: unknown partition %s", e.Seq, d.TP)
	}
	if _, exists := s.PendingByTP[d.TP]; exists {
		return fmt.Errorf("replay: event %d: duplicate revoke for %s", e.Seq, d.TP)
	}
	m := s.Members[d.OldOwner]
	if m == nil {
		return fmt.Errorf("replay: event %d: revoke to unknown member %q", e.Seq, d.OldOwner)
	}
	s.PendingByTP[d.TP] = &protocol.PendingRevoke{
		TP: d.TP, OldOwner: d.OldOwner, NewOwner: d.NewOwner,
		Generation: d.Generation, Deadline: d.Deadline,
	}
	m.Revoking[d.TP] = struct{}{}
	s.PartitionStates[d.TP] = protocol.PSRevoking
	return nil
}

func applyRevokeAcked(s *protocol.GroupState, e protocol.Event, d *protocol.RevokeAckedDetail) error {
	p, ok := s.PendingByTP[d.TP]
	if !ok {
		// Idempotent duplicate of an already-resolved revocation.
		return nil
	}
	if p.Generation != d.Generation || p.OldOwner != d.OldOwner {
		return fmt.Errorf("replay: event %d: ack mismatch for %s", e.Seq, d.TP)
	}
	finishRevocation(s, p)
	s.PartitionStates[d.TP] = protocol.PSRevoked
	recomputePhase(s)
	return nil
}

func applyQuarantined(s *protocol.GroupState, e protocol.Event, d *protocol.RevokeQuarantinedDetail) error {
	p, ok := s.PendingByTP[d.TP]
	if !ok {
		return fmt.Errorf("replay: event %d: quarantine without pending revoke for %s", e.Seq, d.TP)
	}
	if p.Generation != d.Generation || p.OldOwner != d.OldOwner {
		return fmt.Errorf("replay: event %d: quarantine mismatch for %s", e.Seq, d.TP)
	}
	p.QuarantinedAt = d.At
	s.PartitionStates[d.TP] = protocol.PSQuarantined
	s.Phase = protocol.PhaseBlocked
	return nil
}

func applyForceFreed(s *protocol.GroupState, e protocol.Event, d *protocol.PartitionForceFreedDetail) error {
	p, ok := s.PendingByTP[d.TP]
	if !ok {
		return fmt.Errorf("replay: event %d: force-free without pending revoke for %s", e.Seq, d.TP)
	}
	if p.Generation != d.Generation || p.OldOwner != d.OldOwner {
		return fmt.Errorf("replay: event %d: force-free mismatch for %s", e.Seq, d.TP)
	}
	finishRevocation(s, p)
	s.PartitionStates[d.TP] = protocol.PSRevoked
	recomputePhase(s)
	return nil
}

// finishRevocation removes a pending revoke and releases the old owner's
// bookkeeping. It does not touch effective ownership: activation does.
func finishRevocation(s *protocol.GroupState, p *protocol.PendingRevoke) {
	if m := s.Members[p.OldOwner]; m != nil {
		delete(m.Revoking, p.TP)
		if _, owns := m.Owned[p.TP]; owns {
			delete(m.Owned, p.TP)
		}
		delete(s.Owners, p.TP)
		if m.State == protocol.MSRevoking && len(m.Revoking) == 0 {
			m.State = protocol.MSStable
		}
	}
	delete(s.PendingByTP, p.TP)
}

func applyActivated(s *protocol.GroupState, e protocol.Event, d *protocol.GenerationActivatedDetail) error {
	if d.Generation != s.PlannedGeneration {
		return fmt.Errorf("replay: event %d: activate gen %d, planned %d", e.Seq, d.Generation, s.PlannedGeneration)
	}
	if len(s.PendingByTP) != 0 {
		return fmt.Errorf("replay: event %d: activate with %d unresolved revokes", e.Seq, len(s.PendingByTP))
	}
	plan := indexPlan(s.Planned)
	if err := assertAllPartitionsKnown(s, e, grantsTPs(d.Grants)); err != nil {
		return err
	}
	if err := assertAllPartitionsKnown(s, e, d.Unassigned); err != nil {
		return err
	}

	// Clear previous effective ownership wholesale, then install the new one.
	for tp := range s.Owners {
		if m := s.Members[s.Owners[tp].Member]; m != nil {
			delete(m.Owned, tp)
		}
	}
	s.Owners = map[protocol.TP]protocol.Owner{}
	granted := make(map[protocol.TP]bool)
	for _, g := range d.Grants {
		pe, ok := plan[g.TP]
		if !ok {
			return fmt.Errorf("replay: event %d: grant for %s not in plan", e.Seq, g.TP)
		}
		if pe.NewOwner != g.NewOwner {
			return fmt.Errorf("replay: event %d: grant owner mismatch for %s", e.Seq, g.TP)
		}
		m := s.Members[g.NewOwner]
		if m == nil {
			return fmt.Errorf("replay: event %d: grant to unknown member %q", e.Seq, g.NewOwner)
		}
		s.Owners[g.TP] = protocol.Owner{Member: g.NewOwner, Generation: d.Generation}
		m.Owned[g.TP] = struct{}{}
		// A grant that persists into later generations is simply owned.
		s.PartitionStates[g.TP] = protocol.PSOwned
		granted[g.TP] = true
	}
	for _, tp := range d.Unassigned {
		if _, ok := plan[tp]; !ok {
			return fmt.Errorf("replay: event %d: unassigned %s not in plan", e.Seq, tp)
		}
		s.PartitionStates[tp] = protocol.PSUnassigned
		granted[tp] = true
	}
	// Every planned partition must resolve.
	for tp := range plan {
		if !granted[tp] {
			return fmt.Errorf("replay: event %d: plan entry %s missing from activation", e.Seq, tp)
		}
	}
	// Defensive: no partition in the universe may be left in a transitional
	// state after activation.
	for tp, ps := range s.PartitionStates {
		if ps == protocol.PSRevoking || ps == protocol.PSRevoked || ps == protocol.PSQuarantined {
			return fmt.Errorf("replay: event %d: partition %s in state %s after activation", e.Seq, tp, ps)
		}
	}

	s.Generation = d.Generation
	s.Phase = protocol.PhaseStable
	s.Planned = nil
	s.PlannedUnassigned = nil
	s.PlannedGeneration = 0

	// Members: participants are stable; expired members that took no
	// partitions remain expired pending a later sweep/leave.
	for _, m := range s.Members {
		if m.State == protocol.MSPending || m.State == protocol.MSRevoking {
			m.State = protocol.MSStable
		}
		if len(m.Owned) == 0 && m.State != protocol.MSExpired && m.State != protocol.MSLeaving {
			// Subscribes but got nothing (fewer partitions than members):
			// still a stable member.
			m.State = protocol.MSStable
		}
	}
	return nil
}

func applyOffsetCommitted(s *protocol.GroupState, e protocol.Event, d *protocol.OffsetCommittedDetail) error {
	if d.Generation != s.Generation {
		return fmt.Errorf("replay: event %d: commit gen %d, current %d", e.Seq, d.Generation, s.Generation)
	}
	if !knownTP(s, d.TP) {
		return fmt.Errorf("replay: event %d: commit unknown partition %s", e.Seq, d.TP)
	}
	if prev, ok := s.Offsets[d.TP]; ok && d.Offset < prev.Offset {
		return fmt.Errorf("replay: event %d: offset regression %d < %d for %s", e.Seq, d.Offset, prev.Offset, d.TP)
	}
	s.Offsets[d.TP] = protocol.OffsetRecord{
		TP: d.TP, Offset: d.Offset, LeaderEpoch: d.LeaderEpoch,
		Member: d.Member, Generation: d.Generation, CommittedAt: d.At,
	}
	return nil
}

func recomputePhase(s *protocol.GroupState) {
	for _, p := range s.PendingByTP {
		if p.QuarantinedAt.IsZero() {
			s.Phase = protocol.PhaseRevoking
			return
		}
	}
	if s.Phase == protocol.PhaseBlocked {
		s.Phase = protocol.PhaseRevoking
	}
}

func knownTP(s *protocol.GroupState, tp protocol.TP) bool {
	_, ok := s.PartitionStates[tp]
	return ok
}

func assertAllPartitionsKnown(s *protocol.GroupState, e protocol.Event, tps []protocol.TP) error {
	for _, tp := range tps {
		if !knownTP(s, tp) {
			return fmt.Errorf("replay: event %d: unknown partition %s", e.Seq, tp)
		}
	}
	return nil
}

func planTPs(plan []protocol.PlanEntry) []protocol.TP {
	out := make([]protocol.TP, 0, len(plan))
	for _, p := range plan {
		out = append(out, p.TP)
	}
	return out
}

func grantsTPs(grants []protocol.PlanEntry) []protocol.TP {
	out := make([]protocol.TP, 0, len(grants))
	for _, g := range grants {
		out = append(out, g.TP)
	}
	return out
}

func indexPlan(plan []protocol.PlanEntry) map[protocol.TP]protocol.PlanEntry {
	m := make(map[protocol.TP]protocol.PlanEntry, len(plan))
	for _, p := range plan {
		m[p.TP] = p
	}
	return m
}
