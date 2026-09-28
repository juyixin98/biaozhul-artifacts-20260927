package replay

import (
	"fmt"

	"example.com/cgcoord/protocol"
)

// Rebuild reconstructs a group's state purely from its journal. The stored
// state blob is ignored on purpose: the journal is authoritative.
func Rebuild(r EventReader, group string) (*protocol.GroupState, error) {
	stored, err := r.LoadGroup(group)
	if err != nil {
		return nil, err
	}
	if stored == nil {
		return nil, nil
	}
	s := NewEmptyState(group)
	var from protocol.Seq
	const batch = 256
	for {
		events, err := r.ReadEvents(group, from, batch)
		if err != nil {
			return nil, err
		}
		if len(events) == 0 {
			break
		}
		for _, e := range events {
			if e.Seq < from {
				return nil, fmt.Errorf("replay: group %q: seq went backwards: %d < %d", group, e.Seq, from)
			}
			if e.Seq != from {
				return nil, fmt.Errorf("replay: group %q: seq gap: got %d, want %d", group, e.Seq, from)
			}
			if err := Apply(s, e); err != nil {
				return nil, err
			}
			from = e.Seq + 1
		}
		if len(events) < batch {
			break
		}
	}
	return s, nil
}

// Verify rebuilds from the journal and asserts it equals the stored state
// blob. Used at coordinator startup: fail closed rather than serving a state
// whose mutations cannot be replayed.
func Verify(r EventReader, group string) (*protocol.GroupState, error) {
	stored, err := r.LoadGroup(group)
	if err != nil {
		return nil, err
	}
	if stored == nil {
		return nil, nil
	}
	built, err := Rebuild(r, group)
	if err != nil {
		return nil, err
	}
	if diff := Diff(stored, built); diff != "" {
		return nil, fmt.Errorf("replay: group %q: journal/stored mismatch:\n%s", group, diff)
	}
	return built, nil
}

// Diff returns a human-readable, non-exhaustive difference between two
// states, "" when equivalent. Timestamps that the journal carries are
// compared exactly.
func Diff(a, b *protocol.GroupState) string {
	if a == nil || b == nil {
		if a == nil && b == nil {
			return ""
		}
		return "one state is nil"
	}
	var msg string
	add := func(format string, args ...any) {
		msg += "  - " + fmt.Sprintf(format, args...) + "\n"
	}
	if a.Name != b.Name {
		add("name %q vs %q", a.Name, b.Name)
	}
	if a.Generation != b.Generation {
		add("generation %d vs %d", a.Generation, b.Generation)
	}
	if a.Phase != b.Phase {
		add("phase %s vs %s", a.Phase, b.Phase)
	}
	if len(a.Owners) != len(b.Owners) {
		add("owners len %d vs %d", len(a.Owners), len(b.Owners))
	}
	for tp, oa := range a.Owners {
		ob, ok := b.Owners[tp]
		if !ok {
			add("owner %s missing in rebuilt", tp)
			continue
		}
		if oa != ob {
			add("owner %s: %+v vs %+v", tp, oa, ob)
		}
	}
	for tp := range b.Owners {
		if _, ok := a.Owners[tp]; !ok {
			add("owner %s extra in rebuilt", tp)
		}
	}
	if len(a.PendingByTP) != len(b.PendingByTP) {
		add("pending len %d vs %d", len(a.PendingByTP), len(b.PendingByTP))
	}
	for tp, pa := range a.PendingByTP {
		pb, ok := b.PendingByTP[tp]
		if !ok {
			add("pending %s missing in rebuilt", tp)
			continue
		}
		if pa.OldOwner != pb.OldOwner || pa.NewOwner != pb.NewOwner ||
			pa.Generation != pb.Generation || !pa.Deadline.Equal(pb.Deadline) ||
			!pa.QuarantinedAt.Equal(pb.QuarantinedAt) {
			add("pending %s: %+v vs %+v", tp, pa, pb)
		}
	}
	for tp := range b.PendingByTP {
		if _, ok := a.PendingByTP[tp]; !ok {
			add("pending %s extra in rebuilt", tp)
		}
	}
	for tp, psa := range a.PartitionStates {
		if psb := b.PartitionStates[tp]; psa != psb {
			add("partition %s state %s vs %s", tp, psa, psb)
		}
	}
	for tp := range b.PartitionStates {
		if _, ok := a.PartitionStates[tp]; !ok {
			add("partition %s extra in rebuilt", tp)
		}
	}
	if len(a.Members) != len(b.Members) {
		add("members len %d vs %d", len(a.Members), len(b.Members))
	}
	for id, ma := range a.Members {
		mb, ok := b.Members[id]
		if !ok {
			add("member %q missing in rebuilt", id)
			continue
		}
		if ma.State != mb.State {
			add("member %q state %s vs %s", id, ma.State, mb.State)
		}
		if !ma.LastHeartbeat.Equal(mb.LastHeartbeat) {
			add("member %q heartbeat %v vs %v", id, ma.LastHeartbeat, mb.LastHeartbeat)
		}
		if !setEq(ma.Owned, mb.Owned) {
			add("member %q owned %v vs %v", id, ma.Owned, mb.Owned)
		}
		if !setEq(ma.Revoking, mb.Revoking) {
			add("member %q revoking %v vs %v", id, ma.Revoking, mb.Revoking)
		}
	}
	for id := range b.Members {
		if _, ok := a.Members[id]; !ok {
			add("member %q extra in rebuilt", id)
		}
	}
	for tp, oa := range a.Offsets {
		ob, ok := b.Offsets[tp]
		if !ok {
			add("offset %s missing in rebuilt", tp)
			continue
		}
		if oa.Offset != ob.Offset || oa.Member != ob.Member || oa.Generation != ob.Generation {
			add("offset %s: %+v vs %+v", tp, oa, ob)
		}
	}
	for tp := range b.Offsets {
		if _, ok := a.Offsets[tp]; !ok {
			add("offset %s extra in rebuilt", tp)
		}
	}
	return msg
}

func setEq(a, b map[protocol.TP]struct{}) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if _, ok := b[k]; !ok {
			return false
		}
	}
	return true
}
