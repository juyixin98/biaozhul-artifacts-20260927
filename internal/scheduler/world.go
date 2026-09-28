package scheduler

import (
	"placer/internal/model"
)

// cloneWorld builds an independent copy of a view with the same bound
// occupants but no temporary reservations. Used by conflict diagnosis,
// which must not see the partial state of the failed search.
func cloneWorld(src *view) *view {
	nodes := make([]model.Node, len(src.nodes))
	for i, st := range src.nodes {
		nodes[i] = st.n
	}
	var bound []model.Binding
	for _, st := range src.nodes {
		for _, occ := range st.occup {
			if occ.pending {
				continue
			}
			bound = append(bound, model.Binding{
				InstanceID: occ.id,
				NodeID:     st.n.ID,
				Request:    occ.request,
				Groups:     occ.groups,
			})
		}
	}
	rules := make([]model.GroupRule, len(src.gi.rules))
	copy(rules, src.gi.rules)
	w := newView(nodes, bound, model.Policy{Groups: rules}, src.options)
	markEligibility(w, nil) // diagnosis queries carry their own pending view
	return w
}

// newViewNodesFrom is a thin wrapper kept for readability at call sites.
func newViewNodesFrom(src *view, pending []model.Instance) *view {
	w := cloneWorld(src)
	markEligibility(w, pending)
	return w
}
