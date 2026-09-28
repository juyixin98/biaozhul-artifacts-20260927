package kernel

import "fmt"

// Reducer is the authoritative state transition. Recovery after a coordinator
// restart is a pure Fold over the persisted event log; the command path also
// folds each persisted batch, so live and recovered state are computed by the
// same code and cannot diverge.

// Fold rebuilds State for groupID purely from an ordered event slice.
func Fold(groupID string, events []Event) (*State, error) {
	if len(events) == 0 {
		return nil, fmt.Errorf("kernel: no events for group %q", groupID)
	}
	if events[0].Type != EvGroupCreated {
		return nil, fmt.Errorf("kernel: first event for %q must be GROUP_CREATED, got %q", groupID, events[0].Type)
	}
	first := events[0]
	s := NewState(groupID, first.GroupCount, first.SessionTimeout)
	s.Version = first.Version

	for _, e := range events[1:] {
		if err := ApplyEvent(s, e); err != nil {
			return nil, err
		}
	}
	return s, nil
}

// ApplyEvent folds one persisted event into s.
func ApplyEvent(s *State, e Event) error {
	s.Version = e.Version
	switch e.Type {
	case EvGroupCreated:
		return nil // handled by Fold seeding

	case EvMemberJoined:
		addMember(s, e.MemberID, e.At)

	case EvMemberLeft, EvMemberExpired:
		dropMember(s, e.MemberID)

	case EvHeartbeat:
		if m := s.Members[e.MemberID]; m != nil {
			m.LastSeen = e.At
		}

	case EvRebalanceStarted:
		// The plan is the durable decision record. RETAIN actions advance the
		// generation immediately; active->active MOVE actions are executed by
		// the immediately-following REVOCATION_DEMANDED event; direct grants by
		// the following PARTITION_GRANTED event. The group only settles here
		// when this plan contains no ownership moves at all.
		s.Generation = e.Generation
		s.Phase = PhaseRebalancing
		applyPlan(s, e.Plan)
		deriveMemberGens(s)
		if !planHasMoves(e.Plan) && groupSettled(s) {
			s.Phase = PhaseStable
		}

	case EvRevocationDemanded:
		for _, mv := range e.Demanded {
			// STABLE -> REVOKING: old owner remains the valid owner.
			p := s.Partitions[mv.Partition]
			if p.Phase == PStable && p.Owner == mv.OldOwner {
				beginRevoke(s, mv.Partition, mv.OldOwner, mv.OldGen, mv.NewOwner)
			}
		}
		deriveMemberGens(s)

	case EvPartitionRevoked:
		for _, r := range e.Revoke {
			revoke(s, r.Partition, r.OldOwner, r.OldGen, r.NewOwner, e.Force)
		}
		deriveMemberGens(s)

	case EvPartitionGranted:
		for _, g := range e.Grant {
			grant(s, g.Partition, g.NewOwner, g.NewGen)
		}
		deriveMemberGens(s)
		if groupSettled(s) {
			s.Phase = PhaseStable
		}

	case EvOffsetCommitted:
		commitOffset(s, e.CommitPartition, e.CommitOwner, e.CommitGen, e.CommitOffset)

	default:
		return fmt.Errorf("kernel: unknown event type %q", e.Type)
	}
	return nil
}

func planHasMoves(plan []PlanMove) bool {
	for _, m := range plan {
		if m.Action == "MOVE" {
			return true
		}
	}
	return false
}

// applyPlan executes only the RETAIN decisions recorded in a rebalance plan
// (generation advance on unchanged ownership). MOVE decisions are executed by
// the explicit REVOCATION_DEMANDED / PARTITION_REVOKED / PARTITION_GRANTED
// events that follow; the plan is retained as the durable, explainable decision
// record (visible through the replay interface).
func applyPlan(s *State, plan []PlanMove) {
	for _, m := range plan {
		if m.Action != "RETAIN" {
			continue
		}
		p := s.Partitions[m.Partition]
		if p.Phase == PStable && p.Owner == m.NewOwner {
			retain(s, m.Partition, m.NewGen)
		}
	}
}
