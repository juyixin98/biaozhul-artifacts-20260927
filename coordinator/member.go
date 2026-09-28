package coordinator

import (
	"context"
	"sort"

	"example.com/cgcoord/protocol"
	"example.com/cgcoord/storage"
)

// Join adds a member and starts a rebalance. The new member only receives
// ownership when the new generation activates; unchanged ownership is
// retained by the sticky assignment in the kernel.
func (c *Coordinator) Join(ctx context.Context, req JoinRequest) (*JoinResult, error) {
	if req.Member.ID == "" {
		return nil, protocol.NewError(protocol.ErrBadRequest, "member id is required")
	}
	if len(req.Member.Subscription.Topics) == 0 {
		return nil, protocol.NewError(protocol.ErrBadRequest, "member %q must subscribe to at least one topic", req.Member.ID)
	}
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	result := &JoinResult{Member: req.Member.ID}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		if _, exists := s.Members[req.Member.ID]; exists {
			return protocol.NewError(protocol.ErrMemberExists, "member %q already exists", req.Member.ID)
		}
		for _, t := range req.Member.Subscription.Topics {
			if !topicKnown(s, t) {
				return protocol.NewError(protocol.ErrUnknownPartition, "group has no topic %q", t)
			}
		}
		if err := emit(tx, s, protocol.Event{
			Group: s.Name, Type: protocol.EvMemberJoined, At: at, RequestID: req.RequestID,
			Detail: &protocol.MemberJoinedDetail{Member: req.Member, At: at},
		}); err != nil {
			return err
		}
		if err := c.planRebalance(tx, s, "member_join", at, req.RequestID); err != nil {
			return err
		}
		result.Phase = s.Phase
		result.Generation = s.Generation
		if m := s.Members[req.Member.ID]; m != nil {
			result.Revocations = sortedTPs(m.Revoking)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	c.logger.Log("info", "member joined", "request_id", req.RequestID, "group", req.Group,
		"member", req.Member.ID, "phase", result.Phase, "active_generation", result.Generation)
	return result, nil
}

// Leave removes a member in order. Revocations of its partitions are
// auto-confirmed by the leave itself (the member is releasing them by
// definition), so the group progresses without waiting on the leaving
// member's acknowledgements.
func (c *Coordinator) Leave(ctx context.Context, req LeaveRequest) (*LeaveResult, error) {
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	result := &LeaveResult{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		m, ok := s.Members[req.Member]
		if !ok {
			return protocol.NewError(protocol.ErrUnknownMember, "member %q is not a member", req.Member)
		}
		// Mark leaving, then prepare the new generation; the plan revokes
		// every partition this member effectively owns.
		m.State = protocol.MSLeaving
		if err := c.planRebalance(tx, s, "member_leave", at, req.RequestID); err != nil {
			return err
		}
		// Auto-confirm the leaving member's open revocations.
		for _, p := range pendingForOldOwner(s, req.Member) {
			if err := emit(tx, s, protocol.Event{
				Group: s.Name, Type: protocol.EvRevokeAcked, At: at, RequestID: req.RequestID,
				Detail: &protocol.RevokeAckedDetail{
					TP: p.TP, OldOwner: p.OldOwner, Generation: p.Generation, At: at,
				},
			}); err != nil {
				return err
			}
		}
		if err := c.tryActivate(tx, s, at, req.RequestID); err != nil {
			return err
		}
		if err := emit(tx, s, protocol.Event{
			Group: s.Name, Type: protocol.EvMemberLeft, At: at, RequestID: req.RequestID,
			Detail: &protocol.MemberLeftDetail{Member: req.Member, At: at},
		}); err != nil {
			return err
		}
		result.Phase = s.Phase
		result.Generation = s.Generation
		return nil
	})
	if err != nil {
		return nil, err
	}
	c.logger.Log("info", "member left", "request_id", req.RequestID, "group", req.Group,
		"member", req.Member, "active_generation", result.Generation)
	return result, nil
}

// Heartbeat refreshes a member and reports its obligations or assignment.
// A heartbeat naming an old generation is fenced (Kafka-like): it tells the
// member to rejoin rather than silently letting it keep consuming.
func (c *Coordinator) Heartbeat(ctx context.Context, req HeartbeatRequest) (*HeartbeatResult, error) {
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	result := &HeartbeatResult{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		m, ok := s.Members[req.Member]
		if !ok {
			return protocol.NewError(protocol.ErrUnknownMember, "member %q is not a member", req.Member)
		}
		if m.State == protocol.MSLeaving {
			return protocol.NewError(protocol.ErrLeavingMember, "member %q is leaving", req.Member)
		}
		if err := emit(tx, s, protocol.Event{
			Group: s.Name, Type: protocol.EvHeartbeat, At: at, RequestID: req.RequestID,
			Detail: &protocol.HeartbeatDetail{Member: req.Member, At: at},
		}); err != nil {
			return err
		}
		result.Phase = s.Phase
		gen := effectiveOrPlannedGeneration(s)
		result.Generation = gen
		if req.Generation != 0 && req.Generation != gen {
			if req.Generation < gen {
				return protocol.NewError(protocol.ErrStaleGeneration,
					"member %s heartbeat gen %d, current gen %d", req.Member, req.Generation, gen)
			}
			return protocol.NewError(protocol.ErrFutureGeneration,
				"member %s heartbeat gen %d, coordinator at %d", req.Member, req.Generation, gen)
		}
		result.Revoke = sortedTPs(m.Revoking)
		result.Owned = sortedTPs(m.Owned)
		for tp, p := range s.PendingByTP {
			if !p.QuarantinedAt.IsZero() {
				result.Quarantined = append(result.Quarantined, tp)
			}
		}
		result.Quarantined = sortTPs(result.Quarantined)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

// Sync returns a member's assignment in the named generation. It never
// blocks; callers poll. In-flight generations report Stable=false.
func (c *Coordinator) Sync(ctx context.Context, req SyncRequest) (*SyncResult, error) {
	result := &SyncResult{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		m, ok := s.Members[req.Member]
		if !ok {
			return protocol.NewError(protocol.ErrUnknownMember, "member %q is not a member", req.Member)
		}
		_ = m
		result.Generation = s.Generation
		result.Phase = s.Phase
		result.Stable = s.Phase == protocol.PhaseStable &&
			(req.Generation == 0 || req.Generation == s.Generation)
		if result.Stable {
			mm := s.Members[req.Member]
			result.Assignment = sortedTPs(mm.Owned)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

// AckRevocations confirms revocations from the old owner and activates the
// generation when the last one lands.
func (c *Coordinator) AckRevocations(ctx context.Context, req AckRequest) (*AckResult, error) {
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	result := &AckResult{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		if _, ok := s.Members[req.Member]; !ok {
			return protocol.NewError(protocol.ErrUnknownMember, "member %q is not a member", req.Member)
		}
		beforeGen := s.Generation
		for _, tp := range req.Partitions {
			item := c.resolveRevocation(tx, s, req.Member, req.Generation, tp, at, req.RequestID)
			switch item.Outcome {
			case "acked":
				result.Acked = append(result.Acked, item)
			default:
				result.Skipped = append(result.Skipped, item)
			}
		}
		if err := c.tryActivate(tx, s, at, req.RequestID); err != nil {
			return err
		}
		result.Activated = s.Generation > beforeGen
		result.Phase = s.Phase
		result.Generation = s.Generation
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

func topicKnown(s *protocol.GroupState, t protocol.Topic) bool {
	for _, spec := range s.Topics {
		if spec.Name == t {
			return true
		}
	}
	return false
}

func pendingForOldOwner(s *protocol.GroupState, old protocol.MemberID) []*protocol.PendingRevoke {
	out := make([]*protocol.PendingRevoke, 0)
	for _, p := range s.PendingByTP {
		if p.OldOwner == old {
			out = append(out, p)
		}
	}
	sortPending(out)
	return out
}

func effectiveOrPlannedGeneration(s *protocol.GroupState) protocol.Generation {
	if s.Phase == protocol.PhaseStable {
		return s.Generation
	}
	if s.PlannedGeneration != 0 {
		return s.PlannedGeneration
	}
	return s.Generation
}

func sortPending(in []*protocol.PendingRevoke) {
	sort.Slice(in, func(i, j int) bool {
		if in[i].TP.Topic != in[j].TP.Topic {
			return in[i].TP.Topic < in[j].TP.Topic
		}
		return in[i].TP.Partition < in[j].TP.Partition
	})
}

func sortTPs(in []protocol.TP) []protocol.TP {
	sort.Slice(in, func(i, j int) bool {
		if in[i].Topic != in[j].Topic {
			return in[i].Topic < in[j].Topic
		}
		return in[i].Partition < in[j].Partition
	})
	return in
}
