package kernel

import "sort"

// partitionOrigin records why a partition is pre-assigned before balancing.
type partitionOrigin int

const (
	originFree    partitionOrigin = iota // empty / released / orphaned: movable freely
	originStable                         // validly owned by an active member
	originPromise                        // in-flight transfer promised to a member
)

// desiredPartition is the planner's decision for one partition.
type desiredPartition struct {
	id        int
	owner     string // target owner ("" => EMPTY)
	origOwner string // owner before balancing ("" for free partitions)
	origin    partitionOrigin
	locked    bool // promises can never be re-moved during balancing
}

// planInput is derived from State by the engine.
type planInput struct {
	members []string           // sorted active ids
	seeds   []desiredPartition // stable + promise partitions
	free    []int              // partitions available to (re)assign
}

// stickyPlan returns a target owner for every partition that:
//
//  1. preserves unchanged ownership (stable owners and in-flight promises are
//     kept unless load balance strictly requires moving a stable owner),
//  2. moves the minimum number of partitions to balance load to within 1,
//  3. never breaks an in-flight promise (those partitions await revocation),
//  4. is fully deterministic (ties break on ascending id).
//
// Stable owners forced to move during balancing are exactly the active->active
// transfers that go through the revoke/confirm handshake; free partitions
// (empties, departed owners) are granted directly because no live old owner
// exists to release them.
func stickyPlan(in planInput) []desiredPartition {
	members := append([]string(nil), in.members...)
	sort.Strings(members)

	if len(members) == 0 {
		out := make([]desiredPartition, 0, len(in.free))
		for _, p := range in.free {
			out = append(out, desiredPartition{id: p, owner: "", origin: originFree})
		}
		return out
	}

	load := map[string]int{}
	holds := map[string]map[int]*desiredPartition{}
	byID := map[int]*desiredPartition{}
	for _, m := range members {
		holds[m] = map[int]*desiredPartition{}
	}

	seed := func(d desiredPartition) {
		if d.owner != "" {
			load[d.owner]++
			holds[d.owner][d.id] = &d
		}
		byID[d.id] = &d
	}
	for i := range in.seeds {
		seed(in.seeds[i])
	}

	// Assign every free partition to a current minimum-load member.
	free := append([]int(nil), in.free...)
	sort.Ints(free)
	for _, pid := range free {
		m := minLoad(members, load)
		d := desiredPartition{id: pid, owner: m, origOwner: "", origin: originFree}
		load[m]++
		holds[m][pid] = &d
		byID[pid] = &d
	}

	// Balance to within 1. Prefer moving free-origin partitions away from an
	// overloaded member; only relocate a stable owner when unavoidable (that
	// becomes a revoke handshake). Promises are never moved.
	for {
		hi := maxLoad(members, load)
		lo := minLoad(members, load)
		if hi == lo || load[hi]-load[lo] <= 1 {
			break
		}
		var pick *desiredPartition
		// pass 1: free-origin movable
		for _, d := range holds[hi] {
			if d.origin == originFree && d.owner == hi {
				if pick == nil || d.id < pick.id {
					pick = d
				}
			}
		}
		// pass 2: stable-origin (requires revoke)
		if pick == nil {
			for _, d := range holds[hi] {
				if d.origin == originStable && !d.locked && d.owner == hi && d.origOwner == hi {
					if pick == nil || d.id < pick.id {
						pick = d
					}
				}
			}
		}
		if pick == nil {
			break // cannot improve without breaking a promise
		}
		delete(holds[hi], pick.id)
		pick.owner = lo
		holds[lo][pick.id] = pick
		load[hi]--
		load[lo]++
	}

	out := make([]desiredPartition, 0, len(byID))
	for _, d := range byID {
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].id < out[j].id })
	return out
}

func minLoad(members []string, load map[string]int) string {
	best := members[0]
	for _, m := range members[1:] {
		if load[m] < load[best] || (load[m] == load[best] && m < best) {
			best = m
		}
	}
	return best
}

func maxLoad(members []string, load map[string]int) string {
	best := members[0]
	for _, m := range members[1:] {
		if load[m] > load[best] || (load[m] == load[best] && m < best) {
			best = m
		}
	}
	return best
}

func sortStrings(s []string) { sort.Strings(s) }
