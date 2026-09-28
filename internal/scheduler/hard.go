package scheduler

import (
	"fmt"

	"opp284/placement/internal/model"
)

// reservations is the mutable temporary-occupancy state for one solve.
//
// It is exactly the mechanism that prevents simultaneously-placed instances
// from violating one another: every depth-first-search branch owns a copy, so
// resource capacity and affinity/anti-affinity groups see assignments made by
// earlier instances in the same request, while backtracking restores state.
type reservations struct {
	// usage[nodeID] = resources temporarily consumed on top of idx.held.
	usage map[string]model.Resources
	// nodeForInstance maps an intent id already placed in this branch to its
	// chosen node id.
	nodeForInstance map[string]string
	// groupNodes[groupID] = node ids currently occupied by group members
	// (running + pending). For an anti-affinity group the set must never hold
	// duplicates; for an affinity group it must never hold >1 distinct node.
	groupNodes map[string][]string
}

func newReservations() *reservations {
	return &reservations{
		usage:           map[string]model.Resources{},
		nodeForInstance: map[string]string{},
		groupNodes:      map[string][]string{},
	}
}

func (r *reservations) clone() *reservations {
	c := &reservations{
		usage:           make(map[string]model.Resources, len(r.usage)),
		nodeForInstance: make(map[string]string, len(r.nodeForInstance)),
		groupNodes:      make(map[string][]string, len(r.groupNodes)),
	}
	for k, v := range r.usage {
		c.usage[k] = v.Clone()
	}
	for k, v := range r.nodeForInstance {
		c.nodeForInstance[k] = v
	}
	for k, v := range r.groupNodes {
		c.groupNodes[k] = append([]string(nil), v...)
	}
	return c
}

// hardFilter evaluates HARD constraints for intent against every node, given
// a reservations state. Accepted nodes are returned in node-id order; every
// evaluated node is recorded in the returned CandidateSteps (accepted or with
// the specific reject codes).
func hardFilter(in model.Intent, idx *index, res *reservations) ([]model.Node, []CandidateStep) {
	var accepted []model.Node
	var steps []CandidateStep
	for _, n := range idx.nodesSorted {
		step := CandidateStep{NodeID: n.ID, Zone: n.Zone}
		free := n.Capacity.Sub(idx.held[n.ID]).Sub(res.usage[n.ID])
		step.FreeAfter = free
		rejects := nodeHardRejects(in, n, idx, res, free)
		if len(rejects) == 0 {
			step.Accepted = true
			accepted = append(accepted, n)
		} else {
			step.HardRejects = rejects
		}
		steps = append(steps, step)
	}
	return accepted, steps
}

// nodeHardRejects returns the hard-constraint rejects for placing in on n.
//
// Reject codes follow a fixed precedence per node, but every independently
// violated constraint is appended so the failure trace is complete.
func nodeHardRejects(in model.Intent, n model.Node, idx *index, res *reservations, free model.Resources) []model.HardReject {
	var rejects []model.HardReject

	if !n.Eligible {
		rejects = append(rejects, model.HardReject{
			Code: model.ReasonNoEligibleNode, NodeID: n.ID,
			Detail: "node is cordoned/ineligible",
		})
	}
	if in.RequiredZone != "" && n.Zone != in.RequiredZone {
		rejects = append(rejects, model.HardReject{
			Code: model.ReasonZoneMismatch, NodeID: n.ID,
			Detail: fmt.Sprintf("instance pinned to zone %q, node is in %q", in.RequiredZone, n.Zone),
		})
	}
	if !in.NodeSelector.Matches(n.Labels) {
		rejects = append(rejects, model.HardReject{
			Code: model.ReasonSelectorNoMatch, NodeID: n.ID,
			Detail: "node labels do not satisfy node_selector",
		})
	}
	if !free.Fits(in.Request) {
		rejects = append(rejects, model.HardReject{
			Code: model.ReasonInsufficientResource, NodeID: n.ID,
			Detail: fmt.Sprintf("free %v cannot fit request %v (includes simultaneous reservations)", free, in.Request),
		})
	}

	// Group constraints against running AND pending members.
	for _, g := range idx.groupsForIntent[in.ID] {
		occupied := groupOccupied(g.ID, idx, res)
		switch g.Mode {
		case model.GroupAffinity:
			// All members must share one node.
			if len(occupied.nodes) > 0 && !contains(occupied.nodes, n.ID) {
				rejects = append(rejects, model.HardReject{
					Code: model.ReasonAffinityConflict, NodeID: n.ID,
					Detail: fmt.Sprintf("group %s (affinity) members already on node %s", g.ID, occupied.nodes[0]),
				})
			}
		case model.GroupAntiAffinity:
			if contains(occupied.nodes, n.ID) {
				who := occupied.byNode[n.ID]
				rejects = append(rejects, model.HardReject{
					Code: model.ReasonAntiAffinityConflict, NodeID: n.ID,
					Detail: fmt.Sprintf("group %s (anti-affinity) member(s) %v already on this node", g.ID, who),
				})
			}
		}
	}
	return rejects
}

type groupOccupancy struct {
	nodes  []string // distinct node ids
	byNode map[string][]string
}

// groupOccupied computes nodes currently occupied by members of a group:
// running instances plus pending reservations.
func groupOccupied(groupID string, idx *index, res *reservations) groupOccupancy {
	out := groupOccupancy{byNode: map[string][]string{}}
	g := idx.groupsByID[groupID]
	seen := map[string]bool{}
	add := func(nodeID, memberID string) {
		if !seen[nodeID] {
			seen[nodeID] = true
			out.nodes = append(out.nodes, nodeID)
		}
		out.byNode[nodeID] = append(out.byNode[nodeID], memberID)
	}
	for _, m := range g.MemberIDs {
		if r, ok := idx.runningByID[m]; ok {
			add(r.NodeID, m)
		}
		if nodeID, ok := res.nodeForInstance[m]; ok {
			add(nodeID, m)
		}
	}
	return out
}
