package kernel

import "time"

const defaultSessionTimeout = 10 * time.Second

// runRebalance computes a sticky plan for the current membership and returns
// the event batch that drives State to it, MUTATING s through the shared
// transition primitives. The reducer applies the identical moves from the
// persisted events, so live planning and replay converge to the same State.
//
// Event order within the batch:
//
//	REBALANCE_STARTED{plan}  then  PARTITION_REVOKED{moves...}  then
//	PARTITION_GRANTED{grants...}
//
// Safety rule — revoke before grant, never both at once:
//   - an active->active ownership change is emitted as a REVOKING move: the old
//     owner keeps valid ownership and must confirm before the new owner is ever
//     granted. The grant for such a partition is NOT in this batch; it is
//     emitted later by ConfirmRevocation.
//   - a partition whose old owner has departed (or that was empty/orphaned) has
//     no live owner to release it, so it is granted directly in this batch.
//   - in-flight promises are preserved and never re-planned.
func runRebalance(s *State, at time.Time, reqID string) []Event {
	active := s.ActiveMembers()
	s.Generation++
	gen := s.Generation
	s.Phase = PhaseRebalancing

	in := planInput{members: active}
	for _, p := range s.Partitions {
		switch p.Phase {
		case PStable:
			if p.Owner != "" && isActive(s, p.Owner) {
				in.seeds = append(in.seeds, desiredPartition{
					id: p.ID, owner: p.Owner, origOwner: p.Owner,
					origin: originStable,
				})
			} else {
				in.free = append(in.free, p.ID)
			}
		case PRevoking:
			if p.PendingOwner != "" && isActive(s, p.PendingOwner) {
				in.seeds = append(in.seeds, desiredPartition{
					id: p.ID, owner: p.PendingOwner, origOwner: p.PrevOwner,
					origin: originPromise, locked: true,
				})
			}
			// if the planned owner vanished, leave the promise pending; once
			// revoked it will be replanned (still REVOKING => not free here).
		case PPendingGrant:
			if p.PendingOwner != "" && isActive(s, p.PendingOwner) {
				in.seeds = append(in.seeds, desiredPartition{
					id: p.ID, owner: p.PendingOwner, origOwner: "",
					origin: originPromise, locked: true,
				})
			} else {
				p.PendingOwner = ""
				in.free = append(in.free, p.ID)
			}
		case PEmpty:
			in.free = append(in.free, p.ID)
		}
	}

	desired := stickyPlan(in)
	target := make(map[int]string, len(desired))
	for _, d := range desired {
		target[d.id] = d.owner
	}

	var plan []PlanMove
	var demanded []PlanMove // active->active: STABLE -> REVOKING, await confirmation
	var direct []GrantMove  // departed/empty: granted now

	for _, p := range s.Partitions {
		t := target[p.ID]
		switch p.Phase {
		case PStable:
			switch {
			case t == p.Owner && p.Owner != "":
				plan = append(plan, planRetain(p.ID, p.Owner, p.Generation, gen))
				retain(s, p.ID, gen)
			case p.Owner == "" && t != "":
				plan = append(plan, planMove(p.ID, "", t, 0, gen))
				direct = append(direct, GrantMove{Partition: p.ID, NewOwner: t, NewGen: gen})
			case p.Owner != "" && t == "":
				// live owner but no target only occurs when no members remain;
				// that path withdraws owners before planning, so this is dead.
			case p.Owner != "" && t != p.Owner:
				mv := planMove(p.ID, p.Owner, t, p.Generation, gen)
				plan = append(plan, mv)
				demanded = append(demanded, mv)
			}
		case PRevoking:
			// Awaiting confirmation; record the standing move for observability.
			if p.PendingOwner != "" {
				plan = append(plan, planMove(p.ID, p.PrevOwner, p.PendingOwner, p.PrevGeneration, gen))
			} else {
				plan = append(plan, planMove(p.ID, p.PrevOwner, "", p.PrevGeneration, gen))
			}
		case PPendingGrant:
			// Old ownership already withdrawn. Grant to the planned target; when
			// no member remains the target is "" and the partition settles back
			// to an unassigned stable slot (valid owner == "").
			plan = append(plan, planMove(p.ID, "", t, 0, gen))
			direct = append(direct, GrantMove{Partition: p.ID, NewOwner: t, NewGen: gen})
		case PEmpty:
			if t != "" {
				plan = append(plan, planMove(p.ID, "", t, 0, gen))
				direct = append(direct, GrantMove{Partition: p.ID, NewOwner: t, NewGen: gen})
			}
		}
	}

	deriveMemberGens(s)

	var evs []Event
	evs = append(evs, Event{Type: EvRebalanceStarted, At: at, RequestID: reqID, Generation: gen, Plan: plan})
	if len(demanded) > 0 {
		evs = append(evs, Event{Type: EvRevocationDemanded, At: at, RequestID: reqID, Generation: gen, Demanded: demanded})
		for _, mv := range demanded {
			beginRevoke(s, mv.Partition, mv.OldOwner, mv.OldGen, mv.NewOwner)
		}
		deriveMemberGens(s)
	}
	if len(direct) > 0 {
		evs = append(evs, Event{Type: EvPartitionGranted, At: at, RequestID: reqID, Generation: gen, Grant: direct})
		for _, g := range direct {
			grant(s, g.Partition, g.NewOwner, g.NewGen)
		}
	}
	if groupSettled(s) {
		s.Phase = PhaseStable
	}
	return evs
}

// revokeDeparting releases every partition the named (departing) member is the
// valid owner of. It is called BEFORE runRebalance so the plan treats those
// partitions as free and grants them directly in the same batch. force marks
// provenance uncertain (session timeout) versus a clean explicit leave.
func revokeDeparting(s *State, memberID string, force bool) []RevokeMove {
	var out []RevokeMove
	for _, p := range s.Partitions {
		if p.Phase == PRevoking && p.PrevOwner == memberID {
			// already demanded: clean/forced release completes it
			out = append(out, RevokeMove{Partition: p.ID, OldOwner: memberID, NewOwner: p.PendingOwner, OldGen: p.PrevGeneration, NewGen: s.Generation})
			revoke(s, p.ID, memberID, p.PrevGeneration, p.PendingOwner, force)
		} else if p.Phase == PStable && p.Owner == memberID {
			out = append(out, RevokeMove{Partition: p.ID, OldOwner: memberID, NewOwner: "", OldGen: p.Generation, NewGen: s.Generation})
			revoke(s, p.ID, memberID, p.Generation, "", force)
		}
	}
	return out
}

func planRetain(pid int, owner string, oldGen, newGen int64) PlanMove {
	return PlanMove{Partition: pid, Action: "RETAIN", OldOwner: owner, NewOwner: owner, OldGen: oldGen, NewGen: newGen}
}

func planMove(pid int, old, new string, oldGen, newGen int64) PlanMove {
	return PlanMove{Partition: pid, Action: "MOVE", OldOwner: old, NewOwner: new, OldGen: oldGen, NewGen: newGen}
}

// groupSettled reports whether no partition is mid-transfer.
func groupSettled(s *State) bool {
	for _, p := range s.Partitions {
		if p.Phase == PRevoking || p.Phase == PPendingGrant {
			return false
		}
	}
	return true
}

func isActive(s *State, id string) bool {
	_, ok := s.ActiveMember(id)
	return ok
}
