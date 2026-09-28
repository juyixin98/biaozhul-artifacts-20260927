package coordinator

import (
	"context"
	"strconv"

	"example.com/cgcoord/protocol"
	"example.com/cgcoord/replay"
	"example.com/cgcoord/storage"
)

// GetState returns a deep copy of a group's state.
func (c *Coordinator) GetState(ctx context.Context, group string) (*protocol.GroupState, error) {
	var out *protocol.GroupState
	err := c.mutate(ctx, group, func(tx storage.Tx, s *protocol.GroupState) error {
		out = s.Clone()
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// JournalPage reads journal events directly (no state mutation).
func (c *Coordinator) JournalPage(ctx context.Context, group string, from protocol.Seq, limit int) ([]protocol.Event, error) {
	var events []protocol.Event
	err := storage.WithTx(ctx, c.store, func(tx storage.Tx) error {
		if _, err := tx.LoadGroup(group); err == storage.ErrNotFound {
			return protocol.NewError(protocol.ErrUnknownGroup, "group %q does not exist", group)
		} else if err != nil {
			return err
		}
		if limit <= 0 || limit > 1000 {
			limit = 100
		}
		var err error
		events, err = tx.ReadEvents(group, from, limit)
		return err
	})
	if err != nil {
		return nil, err
	}
	return events, nil
}

// RebuildState reconstructs a group purely from its journal. Exposed for the
// replay endpoint and diagnostic tooling.
func (c *Coordinator) RebuildState(ctx context.Context, group string) (*protocol.GroupState, error) {
	built, err := replay.Rebuild(&storeReader{store: c.store, ctx: ctx}, group)
	if err != nil {
		return nil, err
	}
	if built == nil {
		return nil, protocol.NewError(protocol.ErrUnknownGroup, "group %q does not exist", group)
	}
	return built, nil
}

// storeReader adapts the Store to replay.EventReader by opening one read
// transaction per call.
type storeReader struct {
	store storage.Store
	ctx   context.Context
}

func (r *storeReader) context() context.Context {
	if r.ctx != nil {
		return r.ctx
	}
	return context.Background()
}

func (r *storeReader) LoadGroup(name string) (*protocol.GroupState, error) {
	var out *protocol.GroupState
	err := storage.WithTx(r.context(), r.store, func(tx storage.Tx) error {
		s, err := tx.LoadGroup(name)
		if err == storage.ErrNotFound {
			return nil
		}
		if err != nil {
			return err
		}
		out = s
		return nil
	})
	return out, err
}

func (r *storeReader) ReadEvents(group string, from protocol.Seq, limit int) ([]protocol.Event, error) {
	var out []protocol.Event
	err := storage.WithTx(r.context(), r.store, func(tx storage.Tx) error {
		var err error
		out, err = tx.ReadEvents(group, from, limit)
		return err
	})
	return out, err
}

// BuildView renders the explainable external snapshot from internal state.
func BuildView(s *protocol.GroupState) StateView {
	v := StateView{
		Name:       s.Name,
		Generation: s.Generation,
		Phase:      s.Phase,
		Topics:     append([]protocol.TopicSpec(nil), s.Topics...),
	}
	for _, id := range sortedMembers(s.Members) {
		m := s.Members[id]
		topics := append([]protocol.Topic(nil), m.Spec.Subscription.Topics...)
		v.Members = append(v.Members, MemberView{
			ID:            id,
			State:         m.State,
			SubscribesTo:  topics,
			Owned:         sortedTPs(m.Owned),
			Revoking:      sortedTPs(m.Revoking),
			LastHeartbeat: m.LastHeartbeat,
		})
	}
	for _, spec := range v.Topics {
		for i := 0; i < spec.Partitions; i++ {
			tp := protocol.TP{Topic: spec.Name, Partition: protocol.Partition(i)}
			pv := PartitionView{TP: tp, State: s.PartitionStates[tp]}
			if owner, ok := s.Owners[tp]; ok {
				mid := owner.Member
				pv.Owner = &mid
				pv.Generation = owner.Generation
			}
			if p, q := s.PendingByTP[tp]; q {
				if !p.QuarantinedAt.IsZero() {
					pv.Detail = "QUARANTINED since " + p.QuarantinedAt.Format("2006-01-02T15:04:05Z07:00") +
						": revocation by " + string(p.OldOwner) + " never confirmed"
				} else {
					pv.Detail = "revocation issued to " + string(p.OldOwner) + ", deadline " +
						p.Deadline.Format("2006-01-02T15:04:05Z07:00")
				}
			}
			v.Partitions = append(v.Partitions, pv)
		}
	}
	v.Uncertainties = uncertainties(s)
	return v
}

// uncertainties lists conclusions the coordinator cannot make strongly:
// quarantined partitions and force-freed partitions still within the active
// generation. They are reported separately from the happy-path assignment so
// API consumers never mistake an assumption for a fact.
func uncertainties(s *protocol.GroupState) []string {
	out := []string{}
	for _, tp := range sortedPendingTPs(s.PendingByTP) {
		p := s.PendingByTP[tp]
		if p.QuarantinedAt.IsZero() {
			out = append(out,
				"partition "+tp.String()+" has no effective owner: waiting for "+
					string(p.OldOwner)+" to confirm revocation (gen "+strconv.FormatInt(int64(p.Generation), 10)+")")
		} else {
			out = append(out,
				"partition "+tp.String()+" is QUARANTINED: "+string(p.OldOwner)+
					" missed the revocation deadline; ownership outcome uncertain until recovery")
		}
	}
	return out
}
