package scheduler

import (
	"sort"

	"opp284/placement/internal/model"
)

// index precomputes lookup structures for one solve.
type index struct {
	nodeByID      map[string]model.Node
	nodesSorted   []model.Node
	runningByID   map[string]RunningInstance
	runningOnNode map[string][]RunningInstance
	// groupsByID is the group definition table.
	groupsByID map[string]model.Group
	// groupsForIntent maps intent id -> groups it belongs to.
	groupsForIntent map[string][]model.Group
	// held is the baseline resource reservation per node: capacity consumed
	// by running instances. Zero-request intents reserve nothing.
	held map[string]model.Resources
	// allEligibleZones is the set of zones containing >=1 eligible node.
	allEligibleZones map[string]bool
	// existingZones is the set of zones containing >=1 node (eligible or not).
	existingZones map[string]bool
}

func buildIndex(snap Snapshot) *index {
	idx := &index{
		nodeByID:         map[string]model.Node{},
		runningByID:      map[string]RunningInstance{},
		runningOnNode:    map[string][]RunningInstance{},
		groupsByID:       map[string]model.Group{},
		groupsForIntent:  map[string][]model.Group{},
		held:             map[string]model.Resources{},
		allEligibleZones: map[string]bool{},
		existingZones:    map[string]bool{},
	}
	nodes := append([]model.Node(nil), snap.Nodes...)
	sort.Slice(nodes, func(i, j int) bool { return nodes[i].ID < nodes[j].ID })
	idx.nodesSorted = nodes
	for _, n := range nodes {
		idx.nodeByID[n.ID] = n
		idx.existingZones[n.Zone] = true
		if n.Eligible {
			idx.allEligibleZones[n.Zone] = true
		}
		idx.held[n.ID] = n.Used.Clone()
	}
	for _, r := range snap.Running {
		idx.runningByID[r.ID] = r
		idx.runningOnNode[r.NodeID] = append(idx.runningOnNode[r.NodeID], r)
		if n, ok := idx.nodeByID[r.NodeID]; ok {
			idx.held[n.ID] = idx.held[n.ID].Add(r.Request)
		}
	}
	for _, g := range snap.Groups {
		idx.groupsByID[g.ID] = g
		for _, m := range g.MemberIDs {
			idx.groupsForIntent[m] = append(idx.groupsForIntent[m], g)
		}
	}
	return idx
}
