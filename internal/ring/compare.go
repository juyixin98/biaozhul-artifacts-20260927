package ring

import "flowrouter/internal/flow"

// OwnerChange describes one flow whose responsible next-hop differs between
// two ring versions.
type OwnerChange struct {
	FlowKey  string `json:"flow_key"`
	FlowHash uint64 `json:"flow_hash"`
	From     string `json:"from"`
	To       string `json:"to"`
	Reason   string `json:"reason"`
}

// Diff is the minimal-migration audit between two ring versions over a fixed,
// explicit flow set. It records both the consistent-hash result and, for
// comparison, what a full re-modulo baseline would do.
type Diff struct {
	FlowsTotal   int           `json:"flows_total"`
	Stayed       int           `json:"stayed"`
	Moved        int           `json:"moved"`
	MoveFraction float64       `json:"move_fraction"`
	Changes      []OwnerChange `json:"changes"`
	// BaselineModulo is the behavior of hash % len(healthyMembers): nearly
	// every flow is re-rolled when the member count changes. Computed over the
	// exact same flow set so the contrast is apples-to-apples.
	BaselineFlowsTotal int     `json:"baseline_flows_total"`
	BaselineMoved      int     `json:"baseline_moved"`
	BaselineFraction   float64 `json:"baseline_move_fraction"`
}

// memberOrder returns the sorted positive members of a ring, which is the
// canonical slot order of the modulo baseline.
func memberOrder(r *Ring) []string {
	out := make([]string, 0, len(r.alloc.Counts))
	for id, c := range r.alloc.Counts {
		if c > 0 {
			out = append(out, id)
		}
	}
	sortStrings(out)
	return out
}

// moduloOwner maps a hash to a slot under the full-re-modulo baseline.
func moduloOwner(h uint64, ordered []string) string {
	return ordered[h%uint64(len(ordered))]
}

// Compare computes the migration between old and new for the given flows.
//
// Movement reasons are classified so tests can assert the failure/change
// category rather than only counts:
//
//   - "member_removed" : old owner is absent from the new ring;
//   - "member_added"   : old owner still present but the arc was claimed by a
//     newly inserted vnode;
//   - "weight_changed" : both members present but weight-driven vnode changes
//     moved the arc;
//   - "owner_gone_*"   : empty edge rings.
func Compare(old, new_ *Ring, flows []flow.FiveTuple) *Diff {
	d := &Diff{FlowsTotal: len(flows)}
	oldOrder := memberOrder(old)
	newOrder := memberOrder(new_)
	baseOK := len(oldOrder) > 0 && len(newOrder) > 0
	d.BaselineFlowsTotal = len(flows)

	for _, f := range flows {
		h := flowHash(f)
		from, ok1 := old.Lookup(h)
		to, ok2 := new_.Lookup(h)

		switch {
		case ok1 && ok2 && from == to:
			d.Stayed++
		case ok1 && ok2 && from != to:
			d.Moved++
			reason := "weight_changed"
			if !new_.HasMember(from) {
				reason = "member_removed"
			} else if !old.HasMember(to) {
				reason = "member_added"
			}
			d.Changes = append(d.Changes, OwnerChange{
				FlowKey: f.CanonicalKey(), FlowHash: h,
				From: from, To: to, Reason: reason,
			})
		case ok1 && !ok2:
			d.Moved++
			d.Changes = append(d.Changes, OwnerChange{
				FlowKey: f.CanonicalKey(), FlowHash: h,
				From: from, To: "", Reason: "new_ring_empty",
			})
		case !ok1 && ok2:
			d.Moved++
			d.Changes = append(d.Changes, OwnerChange{
				FlowKey: f.CanonicalKey(), FlowHash: h,
				From: "", To: to, Reason: "old_ring_empty",
			})
		}

		if baseOK && moduloOwner(h, oldOrder) != moduloOwner(h, newOrder) {
			d.BaselineMoved++
		}
	}
	if len(flows) > 0 {
		d.MoveFraction = float64(d.Moved) / float64(len(flows))
		if baseOK {
			d.BaselineFraction = float64(d.BaselineMoved) / float64(len(flows))
		}
	}
	return d
}
