package coordinator

import (
	"context"

	"example.com/cgcoord/protocol"
	"example.com/cgcoord/storage"
)

// Commit validates every item against the group state. The commit is
// generation-fenced: the item names the generation the member was assigned
// in, and the coordinator only accepts it when that is the active
// generation, the partition is owned by that member, and revocation has not
// been issued. A slow member holding an old generation is therefore rejected
// explicitly rather than silently overwriting a new owner's progress.
func (c *Coordinator) Commit(ctx context.Context, req CommitRequest) (*CommitResult, error) {
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	result := &CommitResult{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		m, ok := s.Members[req.Member]
		if !ok {
			return protocol.NewError(protocol.ErrUnknownMember, "member %q is not a member", req.Member)
		}
		_ = m
		result.Generation = s.Generation
		for _, item := range req.Items {
			res := CommitResultItem{TP: item.TP, Offset: item.Offset}
			if _, known := s.PartitionStates[item.TP]; !known {
				res.Code, res.Message = protocol.ErrUnknownPartition, "partition is not part of this group"
				result.Items = append(result.Items, res)
				continue
			}
			if s.Phase != protocol.PhaseStable {
				res.Code, res.Message = protocol.ErrRebalanceInProgress,
					"group is in phase " + string(s.Phase)
				result.Items = append(result.Items, res)
				continue
			}
			if req.Generation != s.Generation {
				switch {
				case req.Generation < s.Generation:
					res.Code, res.Message = protocol.ErrStaleGeneration,
						"commit generation is behind the active generation"
				default:
					res.Code, res.Message = protocol.ErrFutureGeneration,
						"commit generation is ahead of the active generation"
				}
				result.Items = append(result.Items, res)
				continue
			}
			owner, owned := s.Owners[item.TP]
			if !owned || owner.Member != req.Member {
				res.Code, res.Message = protocol.ErrNotOwner,
					"member does not own this partition in the named generation"
				result.Items = append(result.Items, res)
				continue
			}
			if p, revoking := s.PendingByTP[item.TP]; revoking && p.OldOwner == req.Member {
				// Belt and suspenders: in STABLE this should not occur, but
				// the fence is explicit.
				res.Code, res.Message = protocol.ErrRevokedPartition,
					"revocation already issued for this partition; late commit refused"
				result.Items = append(result.Items, res)
				continue
			}
			if item.Offset < 0 {
				res.Code, res.Message = protocol.ErrBadRequest, "offset must be >= 0"
				result.Items = append(result.Items, res)
				continue
			}
			if prev, exists := s.Offsets[item.TP]; exists && item.Offset < prev.Offset {
				res.Code, res.Message = protocol.ErrOffsetRegression,
					"offset below previously committed offset"
				result.Items = append(result.Items, res)
				continue
			}
			epoch := item.LeaderEpoch
			if epoch == 0 {
				epoch = -1
			}
			if err := emit(tx, s, protocol.Event{
				Group: s.Name, Type: protocol.EvOffsetCommitted, At: at, RequestID: req.RequestID,
				Detail: &protocol.OffsetCommittedDetail{
					TP: item.TP, Offset: item.Offset, LeaderEpoch: epoch,
					Member: req.Member, Generation: s.Generation, At: at,
				},
			}); err != nil {
				return err
			}
			res.Committed = true
			result.Items = append(result.Items, res)
		}
		c.logger.Log("info", "offsets committed", "request_id", req.RequestID, "group", s.Name,
			"member", req.Member, "requested_generation", req.Generation,
			"active_generation", s.Generation, "items", len(result.Items))
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

// HasRejections reports whether at least one item was rejected.
func (r *CommitResult) HasRejections() bool {
	for _, it := range r.Items {
		if !it.Committed {
			return true
		}
	}
	return false
}

// FetchOffsets returns committed offsets for the requested partitions (all
// known partitions when the request lists none).
func (c *Coordinator) FetchOffsets(ctx context.Context, req FetchOffsetsRequest) (map[protocol.TP]protocol.OffsetRecord, error) {
	out := map[protocol.TP]protocol.OffsetRecord{}
	err := c.mutate(ctx, req.Group, func(tx storage.Tx, s *protocol.GroupState) error {
		if len(req.Partitions) == 0 {
			for tp, rec := range s.Offsets {
				out[tp] = rec
			}
			return nil
		}
		for _, tp := range req.Partitions {
			if _, known := s.PartitionStates[tp]; !known {
				return protocol.NewError(protocol.ErrUnknownPartition, "partition %s is not part of this group", tp)
			}
			if rec, ok := s.Offsets[tp]; ok {
				out[tp] = rec
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}
