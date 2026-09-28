package coordinator

import (
	"time"

	"example.com/cgcoord/kernel"
	"example.com/cgcoord/protocol"
	"example.com/cgcoord/storage"
)

// planRebalance computes the next generation from current state and stages
// it. It emits PREPARE_REBALANCE plus REVOKE_ISSUED events for every
// partition that must change hands. No grant happens here: the generation
// activates only after every revocation is confirmed (or force-freed).
//
// Caller guarantees: phase is STABLE or EMPTY, and the caller has already
// appended/validated the triggering event (join/leave/expire/recover).
func (c *Coordinator) planRebalance(
	tx storage.Tx,
	s *protocol.GroupState,
	reason string,
	at time.Time,
	requestID string,
) error {
	members := planningMembers(s)
	desired := kernel.Assign(kernel.AssignInput{
		Topics:   s.Topics,
		Members:  members,
		Previous: ownerMembers(s.Owners),
	}).Owners
	plan := kernel.DiffPlan(s.Topics, desired, ownerMembers(s.Owners))

	newGen := s.Generation + 1
	entries := make([]protocol.PlanEntry, 0, len(plan.Entries))
	for _, e := range plan.Entries {
		entries = append(entries, protocol.PlanEntry{
			TP: e.TP, OldOwner: e.OldOwner, NewOwner: e.NewOwner, Retained: e.Retained,
		})
	}
	if err := emit(tx, s, protocol.Event{
		Group: s.Name, Type: protocol.EvPrepareRebalance, At: at, RequestID: requestID,
		Detail: &protocol.PrepareRebalanceDetail{Generation: newGen, Reason: reason, Plan: entries},
	}); err != nil {
		return err
	}

	for _, e := range plan.Changed() {
		if e.OldOwner == "" {
			// Free partition (no previous owner): nothing to revoke.
			continue
		}
		if err := emit(tx, s, protocol.Event{
			Group: s.Name, Type: protocol.EvRevokeIssued, At: at, RequestID: requestID,
			Detail: &protocol.RevokeIssuedDetail{
				TP: e.TP, OldOwner: e.OldOwner, NewOwner: e.NewOwner,
				Generation: newGen, Deadline: at.Add(s.Config.RevokeTimeout),
			},
		}); err != nil {
			return err
		}
	}

	return c.tryActivate(tx, s, at, requestID)
}

// planningMembers returns the MemberSpecs that the next generation is
// computed for: members that joined and have not left or expired. A member
// that is merely revoking (heartbeats current, outstanding revocations)
// remains eligible.
func planningMembers(s *protocol.GroupState) []protocol.MemberSpec {
	out := make([]protocol.MemberSpec, 0, len(s.Members))
	for _, m := range s.Members {
		if m.State == protocol.MSLeaving || m.State == protocol.MSExpired {
			continue
		}
		out = append(out, m.Spec)
	}
	return out
}

// tryActivate completes the planned generation if every revocation has been
// resolved (acked, expired-and-force-freed, or inherently free). It emits
// GENERATION_ACTIVATED with the full grant list.
func (c *Coordinator) tryActivate(tx storage.Tx, s *protocol.GroupState, at time.Time, requestID string) error {
	if s.Phase != protocol.PhaseRevoking {
		return nil // BLOCKED: at least one partition quarantined
	}
	if len(s.PendingByTP) != 0 {
		return nil
	}
	gen := s.PlannedGeneration
	grants := make([]protocol.PlanEntry, 0, len(s.Planned))
	unassigned := make([]protocol.TP, 0)
	for _, pe := range s.Planned {
		if pe.NewOwner == "" {
			unassigned = append(unassigned, pe.TP)
			continue
		}
		grants = append(grants, pe)
	}
	if err := emit(tx, s, protocol.Event{
		Group: s.Name, Type: protocol.EvGenerationActivated, At: at, RequestID: requestID,
		Detail: &protocol.GenerationActivatedDetail{
			Generation: gen, Grants: grants, Unassigned: unassigned,
		},
	}); err != nil {
		return err
	}
	c.logger.Log("info", "generation activated", "request_id", requestID, "group", s.Name,
		"generation", gen, "grants", len(grants), "unassigned", len(unassigned))
	return nil
}

// resolveRevocation applies one revocation confirmation. It tolerates
// unknown/stale partitions (reported categorically) and returns whether the
// confirmation advanced the group.
func (c *Coordinator) resolveRevocation(
	tx storage.Tx,
	s *protocol.GroupState,
	member protocol.MemberID,
	gen protocol.Generation,
	tp protocol.TP,
	at time.Time,
	requestID string,
) AckItem {
	if _, known := s.PartitionStates[tp]; !known {
		return AckItem{TP: tp, Outcome: "unknown", Reason: "partition is not part of this group"}
	}
	p, ok := s.PendingByTP[tp]
	if !ok {
		// Already resolved in this planned generation, or no revocation
		// targets it: duplicate or stale confirmation.
		switch s.PartitionStates[tp] {
		case protocol.PSRevoked:
			return AckItem{TP: tp, Outcome: "stale", Reason: "revocation already confirmed"}
		case protocol.PSQuarantined:
			return AckItem{TP: tp, Outcome: "quarantined", Reason: "use the recovery endpoint for a quarantined partition"}
		default:
			return AckItem{TP: tp, Outcome: "stale", Reason: "no open revocation for this partition"}
		}
	}
	if gen != 0 && gen != p.Generation {
		return AckItem{TP: tp, Outcome: "stale", Reason: "generation does not match the open revocation"}
	}
	if member != "" && member != p.OldOwner {
		return AckItem{TP: tp, Outcome: "stale", Reason: "only the old owner can confirm this revocation"}
	}
	if err := emit(tx, s, protocol.Event{
		Group: s.Name, Type: protocol.EvRevokeAcked, At: at, RequestID: requestID,
		Detail: &protocol.RevokeAckedDetail{
			TP: tp, OldOwner: p.OldOwner, Generation: p.Generation, At: at,
		},
	}); err != nil {
		return AckItem{TP: tp, Outcome: "unknown", Reason: err.Error()}
	}
	c.logger.Log("info", "revocation confirmed", "request_id", requestID, "group", s.Name,
		"partition", tp.String(), "old_owner", p.OldOwner, "new_owner", p.NewOwner,
		"generation", p.Generation)
	return AckItem{TP: tp, Outcome: "acked"}
}
