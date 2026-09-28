package kernel

import (
	"sort"

	"example.com/cgcoord/protocol"
)

// PlanEntry is one partition transition in a rebalance, in a form the
// coordinator can turn into revocation and grant events.
type PlanEntry struct {
	TP       protocol.TP
	OldOwner protocol.MemberID
	NewOwner protocol.MemberID
	// Retained means ownership is unchanged: no revocation, no grant delay.
	Retained bool
}

// Plan is the full revoke-before-grant diff for a new generation.
type Plan struct {
	Entries []PlanEntry
}

// Changed returns the entries whose ownership moves (old -> new), including
// moves to/from the empty owner. Retained entries are excluded.
func (p Plan) Changed() []PlanEntry {
	out := make([]PlanEntry, 0, len(p.Entries))
	for _, e := range p.Entries {
		if !e.Retained {
			out = append(out, e)
		}
	}
	return out
}

// DiffPlan builds the transition plan from the current effective ownership to
// the desired assignment.
//
// The invariant this enforces: a partition that changes hands appears first
// as a revocation of the old owner; the new owner is granted only after the
// revocation is confirmed. Retained partitions carry no revoke/grant. The
// partition universe is taken from topics: partitions disappearing from a
// topic spec are simply absent.
func DiffPlan(topics []protocol.TopicSpec, desired map[protocol.TP]protocol.MemberID, current map[protocol.TP]protocol.MemberID) Plan {
	entries := make([]PlanEntry, 0)
	for _, topic := range sortedTopics(topics) {
		for i := 0; i < topic.Partitions; i++ {
			tp := protocol.TP{Topic: topic.Name, Partition: protocol.Partition(i)}
			old := current[tp]
			neu := desired[tp]
			e := PlanEntry{TP: tp, OldOwner: old, NewOwner: neu}
			e.Retained = old != "" && old == neu
			entries = append(entries, e)
		}
	}
	sort.Slice(entries, func(i, j int) bool { return lessTP(entries[i].TP, entries[j].TP) })
	return Plan{Entries: entries}
}
