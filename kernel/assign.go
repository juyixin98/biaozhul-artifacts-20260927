// Package kernel contains the pure decision functions of the coordinator:
// sticky partition assignment and the revoke-before-grant plan diff. It has
// no state, no clock and no I/O, which makes it independently testable and
// lets the independent test oracle re-derive expected results from the same
// specifications rather than from the coordinator's own state.
package kernel

import (
	"sort"

	"example.com/cgcoord/protocol"
)

// AssignInput is the input to a sticky assignment computation.
type AssignInput struct {
	// Topics declares the partition universe.
	Topics []protocol.TopicSpec
	// Members are the members the generation is computed for: joined,
	// subscribed, non-leaving members. Members leaving or expired must be
	// excluded by the caller so nothing gets retained to them.
	Members []protocol.MemberSpec
	// Previous maps every currently-effective ownership from the active
	// generation. The assignment tries to keep it.
	Previous map[protocol.TP]protocol.MemberID
}

// AssignOutput is the computed desired ownership.
type AssignOutput struct {
	// Owners maps every partition of the universe to its desired owner.
	// Partitions with no eligible member are absent.
	Owners map[protocol.TP]protocol.MemberID
}

// Assign computes a deterministic, balanced, sticky assignment.
//
// Guarantees, per topic independently:
//   - every partition gets at most one owner, and an owner only if it
//     subscribes to the topic;
//   - owner loads differ by at most one partition;
//   - the number of ownership changes versus Previous is minimized:
//     existing ownership is retained up to the per-member ceiling, and
//     released partitions are taken only to lift a below-floor member;
//   - the result is fully deterministic in member IDs and partition order
//     (sorted), so two coordinators given the same input agree.
func Assign(in AssignInput) AssignOutput {
	out := AssignOutput{Owners: make(map[protocol.TP]protocol.MemberID)}

	for _, topic := range sortedTopics(in.Topics) {
		assignTopic(topic, in.Members, in.Previous, out.Owners)
	}
	return out
}

func assignTopic(
	topic protocol.TopicSpec,
	members []protocol.MemberSpec,
	previous map[protocol.TP]protocol.MemberID,
	out map[protocol.TP]protocol.MemberID,
) {
	eligible := make([]protocol.MemberID, 0, len(members))
	subs := make(map[protocol.MemberID]bool)
	for _, m := range members {
		if m.Subscription.Subscribes(topic.Name) {
			eligible = append(eligible, m.ID)
			subs[m.ID] = true
		}
	}
	sort.Slice(eligible, func(i, j int) bool { return eligible[i] < eligible[j] })
	eligible = dedupMembers(eligible)
	if len(eligible) == 0 {
		return // partitions stay unassigned
	}

	parts := make([]protocol.TP, topic.Partitions)
	for i := 0; i < topic.Partitions; i++ {
		parts[i] = protocol.TP{Topic: topic.Name, Partition: protocol.Partition(i)}
	}
	sort.Slice(parts, func(i, j int) bool { return lessTP(parts[i], parts[j]) })

	n := len(eligible)
	ceil := (len(parts) + n - 1) / n
	floor := len(parts) / n

	load := make(map[protocol.MemberID]int, n)
	for _, id := range eligible {
		load[id] = 0
	}

	// Pass 1: retain unchanged ownership, in deterministic partition order,
	// up to the ceiling.
	free := make([]protocol.TP, 0)
	for _, tp := range parts {
		old, had := previous[tp]
		if had && subs[old] && load[old] < ceil {
			out[tp] = old
			load[old]++
			continue
		}
		free = append(free, tp)
	}

	// Pass 2: move pass. If a member is below floor, take one partition from
	// the most-loaded member (tie broken by greater member ID) and put it in
	// the free pool. This is the only case in which unchanged ownership is
	// broken, and it runs only when balancing requires it.
	for {
		giver := pickGiver(load, eligible, ceil)
		taker := pickTaker(load, eligible, floor)
		if giver == "" || taker == "" {
			break
		}
		tp := takeOne(topic, out, giver)
		delete(out, tp)
		load[giver]--
		free = append(free, tp)
	}

	// Pass 3: hand free partitions to the lowest-loaded member, tie broken by
	// smaller member ID.
	for _, tp := range freeSort(free) {
		m := pickLowest(load, eligible, ceil)
		if m == "" {
			break // everyone at ceiling (shouldn't happen)
		}
		out[tp] = m
		load[m]++
	}
}

func pickGiver(load map[protocol.MemberID]int, eligible []protocol.MemberID, ceil int) protocol.MemberID {
	var giver protocol.MemberID
	best := 0
	for _, id := range eligible { // ascending IDs
		if load[id] >= ceil && load[id] >= best {
			giver, best = id, load[id]
		}
	}
	return giver
}

func pickTaker(load map[protocol.MemberID]int, eligible []protocol.MemberID, floor int) protocol.MemberID {
	for _, id := range eligible { // ascending IDs: smallest ID below floor first
		if load[id] < floor {
			return id
		}
	}
	return ""
}

func pickLowest(load map[protocol.MemberID]int, eligible []protocol.MemberID, ceil int) protocol.MemberID {
	var best protocol.MemberID
	bestLoad := 0
	found := false
	for _, id := range eligible {
		if load[id] >= ceil {
			continue
		}
		if !found || load[id] < bestLoad {
			best, bestLoad, found = id, load[id], true
		}
	}
	return best
}

func takeOne(topic protocol.TopicSpec, owners map[protocol.TP]protocol.MemberID, giver protocol.MemberID) protocol.TP {
	// Take the greatest partition index the giver owns in this topic, so the
	// choice is deterministic.
	var best protocol.TP
	found := false
	for tp, m := range owners {
		if tp.Topic != topic.Name || m != giver {
			continue
		}
		if !found || tp.Partition > best.Partition {
			best, found = tp, true
		}
	}
	return best
}

func freeSort(free []protocol.TP) []protocol.TP {
	sort.Slice(free, func(i, j int) bool { return lessTP(free[i], free[j]) })
	return free
}

func sortedTopics(topics []protocol.TopicSpec) []protocol.TopicSpec {
	out := append([]protocol.TopicSpec(nil), topics...)
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func dedupMembers(in []protocol.MemberID) []protocol.MemberID {
	if len(in) < 2 {
		return in
	}
	out := in[:1]
	for _, id := range in[1:] {
		if id != out[len(out)-1] {
			out = append(out, id)
		}
	}
	return out
}

func lessTP(a, b protocol.TP) bool {
	if a.Topic != b.Topic {
		return a.Topic < b.Topic
	}
	return a.Partition < b.Partition
}
