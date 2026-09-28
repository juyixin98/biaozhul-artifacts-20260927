package coordinator

import (
	"context"
	"sort"
	"time"

	"example.com/cgcoord/protocol"
	"example.com/cgcoord/storage"
)

// SweepResult reports everything one sweep did, for tests and logs.
type SweepResult struct {
	Expired     []protocol.MemberID
	Quarantined []protocol.TP
	ForceFreed  []protocol.TP
	Activated   []protocol.Generation
	Phase       protocol.Phase
	Generation  protocol.Generation
}

// Sweep advances wall-clock-driven state for one group:
//
//  1. members past their session timeout are expired; their still-owned
//     partitions enter the same revoke/quarantine machinery;
//  2. revocations past RevokeTimeout are QUARANTINED: the partition has no
//     effective owner and the planned generation cannot activate;
//  3. partitions past QuarantineTimeout are force-freed (the recorded
//     uncertainty is PARTITION_FORCE_FREED in the journal). Once every
//     partition for a blocked generation is freed, the generation activates
//     in the same sweep.
func (c *Coordinator) Sweep(ctx context.Context, group string, at time.Time) (*SweepResult, error) {
	if at.IsZero() {
		at = c.now()
	}
	res := &SweepResult{}
	err := c.mutate(ctx, group, func(tx storage.Tx, s *protocol.GroupState) error {
		// 1. expire dead members.
		for _, id := range sortedMembers(s.Members) {
			m := s.Members[id]
			if m.State == protocol.MSExpired || m.State == protocol.MSLeaving {
				continue
			}
			timeout := m.Spec.SessionTimeout
			if timeout == 0 {
				timeout = s.Config.DefaultSessionTimeout
			}
			if at.Sub(m.LastHeartbeat) <= timeout {
				continue
			}
			if err := emit(tx, s, protocol.Event{
				Group: s.Name, Type: protocol.EvMemberExpired, At: at, RequestID: "sweep",
				Detail: &protocol.MemberExpiredDetail{Member: id, At: at},
			}); err != nil {
				return err
			}
			res.Expired = append(res.Expired, id)
			// Re-plan without the expired member when a generation is not
			// already in flight. If one is in flight, its revocations
			// already cover partitions the expired member owns; the member
			// simply cannot ack and the quarantine path handles them.
			if s.Phase == protocol.PhaseStable || s.Phase == protocol.PhaseEmpty {
				if err := c.planRebalance(tx, s, "member_expired", at, "sweep"); err != nil {
					return err
				}
			}
		}

		// 2 + 3. resolve timed-out revocations.
		for _, tp := range sortedPendingTPs(s.PendingByTP) {
			p := s.PendingByTP[tp]
			if p.QuarantinedAt.IsZero() && !at.Before(p.Deadline) {
				if err := emit(tx, s, protocol.Event{
					Group: s.Name, Type: protocol.EvRevokeQuarantined, At: at, RequestID: "sweep",
					Detail: &protocol.RevokeQuarantinedDetail{
						TP: tp, OldOwner: p.OldOwner, Generation: p.Generation, At: at,
					},
				}); err != nil {
					return err
				}
				res.Quarantined = append(res.Quarantined, tp)
				c.logger.Log("warn", "revocation not confirmed; partition quarantined",
					"request_id", "sweep", "group", s.Name, "partition", tp.String(),
					"old_owner", p.OldOwner, "generation", p.Generation, "deadline", p.Deadline)
			}
		}
		for _, tp := range sortedPendingTPs(s.PendingByTP) {
			p := s.PendingByTP[tp]
			if p.QuarantinedAt.IsZero() {
				continue
			}
			if at.Sub(p.QuarantinedAt) < s.Config.QuarantineTimeout {
				continue
			}
			if err := emit(tx, s, protocol.Event{
				Group: s.Name, Type: protocol.EvPartitionForceFreed, At: at, RequestID: "sweep",
				Detail: &protocol.PartitionForceFreedDetail{
					TP: tp, OldOwner: p.OldOwner, Generation: p.Generation,
					Reason: "quarantine timeout elapsed; sweep assumed old owner stopped", At: at,
				},
			}); err != nil {
				return err
			}
			res.ForceFreed = append(res.ForceFreed, tp)
			c.logger.Log("warn", "quarantined partition force-freed (uncertain ownership assumed stopped)",
				"request_id", "sweep", "group", s.Name, "partition", tp.String(),
				"old_owner", p.OldOwner, "generation", p.Generation)
		}

		before := s.Generation
		if err := c.tryActivate(tx, s, at, "sweep"); err != nil {
			return err
		}
		if s.Generation > before {
			res.Activated = append(res.Activated, s.Generation)
		}
		res.Phase = s.Phase
		res.Generation = s.Generation
		return nil
	})
	if err != nil {
		return nil, err
	}
	return res, nil
}

// RecoverAck is the out-of-band path for quarantined partitions: an operator
// tool confirms the old owner has stopped, and the coordinator releases the
// partition and activates the waiting generation. It is deliberately separate
// from ordinary revoke acks so "we assumed the member stopped" never happens
// silently.
func (c *Coordinator) RecoverAck(ctx context.Context, req RecoverAckRequest) (*AckResult, error) {
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	result := &AckResult{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		before := s.Generation
		for _, tp := range req.Partitions {
			p, ok := s.PendingByTP[tp]
			if !ok {
				result.Skipped = append(result.Skipped, AckItem{
					TP: tp, Outcome: "stale", Reason: "no open revocation for this partition",
				})
				continue
			}
			if req.Generation != 0 && req.Generation != p.Generation {
				result.Skipped = append(result.Skipped, AckItem{
					TP: tp, Outcome: "stale", Reason: "generation does not match the open revocation",
				})
				continue
			}
			if req.Member != "" && req.Member != p.OldOwner {
				result.Skipped = append(result.Skipped, AckItem{
					TP: tp, Outcome: "stale", Reason: "recovery ack names a different owner",
				})
				continue
			}
			if p.QuarantinedAt.IsZero() {
				// Not timed out yet: treat as a normal ack.
				item := c.resolveRevocation(tx, s, p.OldOwner, p.Generation, tp, at, req.RequestID)
				if item.Outcome == "acked" {
					result.Acked = append(result.Acked, item)
				} else {
					result.Skipped = append(result.Skipped, item)
				}
				continue
			}
			if err := emit(tx, s, protocol.Event{
				Group: s.Name, Type: protocol.EvPartitionForceFreed, At: at, RequestID: req.RequestID,
				Detail: &protocol.PartitionForceFreedDetail{
					TP: tp, OldOwner: p.OldOwner, Generation: p.Generation,
					Reason: "out-of-band recovery: " + req.Reason, At: at,
				},
			}); err != nil {
				return err
			}
			result.Acked = append(result.Acked, AckItem{TP: tp, Outcome: "recovered", Reason: req.Reason})
			c.logger.Log("warn", "partition recovered out of band",
				"request_id", req.RequestID, "group", s.Name, "partition", tp.String(),
				"old_owner", p.OldOwner, "generation", p.Generation, "reason", req.Reason)
		}
		if err := c.tryActivate(tx, s, at, req.RequestID); err != nil {
			return err
		}
		result.Activated = s.Generation > before
		result.Phase = s.Phase
		result.Generation = s.Generation
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

func sortedMembers(members map[protocol.MemberID]*protocol.MemberRuntime) []protocol.MemberID {
	out := make([]protocol.MemberID, 0, len(members))
	for id := range members {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func sortedPendingTPs(pending map[protocol.TP]*protocol.PendingRevoke) []protocol.TP {
	out := make([]protocol.TP, 0, len(pending))
	for tp := range pending {
		out = append(out, tp)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Topic != out[j].Topic {
			return out[i].Topic < out[j].Topic
		}
		return out[i].Partition < out[j].Partition
	})
	return out
}
