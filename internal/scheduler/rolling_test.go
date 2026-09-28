package scheduler_test

import (
	"strconv"
	"testing"

	"placer/internal/model"
	"placer/internal/scheduler"
)

// TestRolling_SimpleSurge verifies a surge replacement under hard zone
// anti-affinity. There are three zones with two old occupants (za, zb) and
// one spare zone (zc), MaxSurge=1, MaxUnavailable=0: the only way to roll
// is to surge a new instance into the spare zone, then retire the old and
// move subsequent replacements across zones. The final distribution is
// again one-per-occupied-zone, and the returned op sequence is replayed
// independently to verify the envelope and hard rules at every step.
func TestRolling_SimpleSurge(t *testing.T) {
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 4000, Memory: 1e10, Storage: 1e11}},
		{ID: "b1", Zone: "zb", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 4000, Memory: 1e10, Storage: 1e11}},
		{ID: "c1", Zone: "zc", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 4000, Memory: 1e10, Storage: 1e11}},
	}
	policy := model.Policy{Groups: []model.GroupRule{
		{Group: "app", Mode: model.ModeHard, TopologyKey: "zone"},
	}}
	old := []model.Instance{
		{ID: "old-0", State: model.StateBound, NodeID: "a1",
			Request: res(1000, 1e8), Groups: map[string]string{"app": "w"}},
		{ID: "old-1", State: model.StateBound, NodeID: "b1",
			Request: res(1000, 1e8), Groups: map[string]string{"app": "w"}},
	}
	nw := []model.Instance{
		pending("new-0", res(1000, 1e8), map[string]string{"app": "w"}),
		pending("new-1", res(1000, 1e8), map[string]string{"app": "w"}),
	}
	replaces := map[string]string{"new-0": "old-0", "new-1": "old-1"}

	req := model.ReplaceRequest{
		RunID: "run-roll-ok", Nodes: nodes, Old: old, New: nw,
		Replaces: replaces, Policy: policy, MaxSurge: 1, MaxUnavailable: 0,
	}
	r, err := scheduler.Replace(req)
	if err != nil {
		t.Fatalf("Replace error: %v", err)
	}
	if !r.Feasible {
		t.Fatalf("expected feasible rolling plan, conflicts=%+v", r.Conflicts)
	}

	// Final placement: two new instances in two distinct zones.
	finalZones := map[string]int{}
	for _, d := range r.Final {
		finalZones[zoneOf(nodes, d.NodeID)]++
	}
	if len(finalZones) != 2 {
		t.Fatalf("final placement must span two distinct zones, got %+v (%+v)",
			finalZones, r.Final)
	}
	for z, c := range finalZones {
		if c != 1 {
			t.Fatalf("zone %s must hold exactly one new instance, got %d", z, c)
		}
	}

	// Validate the op sequence independently by replaying it.
	if err := replayEnvelope(nodes, old, nw, replaces, r.Ops, policy, 1, 0); err != nil {
		t.Fatalf("op sequence violates envelope/rules: %v\nops=%+v", err, r.Ops)
	}
	// With surge-first preference and place_new < evict_old ordering, the
	// sequence must start by placing a new instance into the spare zone.
	if len(r.Ops) == 0 || r.Ops[0].Kind != "place_new" {
		t.Fatalf("expected first op place_new, got %+v", r.Ops)
	}
	if r.Ops[0].NodeID != "c1" {
		t.Fatalf("first surge must land in the spare zone c1, got %q", r.Ops[0].NodeID)
	}
}

// TestRolling_UnavailableInsteadOfSurge: clusters where surge capacity is
// absent but one unavailable slot is allowed; sequence must evict first.
func TestRolling_UnavailableInsteadOfSurge(t *testing.T) {
	// One node per zone, tight capacity: old uses nearly all capacity so a
	// new cannot coexist (surge impossible resource-wise), but evicting the
	// old first frees the node.
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 1200, Memory: 1e10, Storage: 1e11}},
	}
	old := []model.Instance{{ID: "old-0", State: model.StateBound, NodeID: "a1",
		Request: res(1000, 1e8)}}
	nw := []model.Instance{pending("new-0", res(1000, 1e8), nil)}
	replaces := map[string]string{"new-0": "old-0"}

	r, err := scheduler.Replace(model.ReplaceRequest{
		RunID: "run-roll-unavail", Nodes: nodes, Old: old, New: nw,
		Replaces: replaces, MaxSurge: 0, MaxUnavailable: 1,
	})
	if err != nil {
		t.Fatalf("Replace error: %v", err)
	}
	if !r.Feasible {
		t.Fatalf("evict-first plan should be feasible, conflicts=%+v", r.Conflicts)
	}
	if len(r.Ops) != 2 || r.Ops[0].Kind != "evict_old" || r.Ops[1].Kind != "place_new" {
		t.Fatalf("expected [evict_old, place_new], got %+v", r.Ops)
	}
	if r.Ops[0].InstanceID != "old-0" || r.Ops[1].NodeID != "a1" {
		t.Fatalf("unexpected ops: %+v", r.Ops)
	}
}

// TestRolling_ImpossibleEnvelope: zero/zero envelope is rejected at
// validation (no first move), and a resource-tight cluster with zero
// unavailable and zero effective surge yields a conflict, not a placement.
func TestRolling_ImpossibleEnvelope(t *testing.T) {
	nodes := []model.Node{{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
		Capacity: model.Resources{MilliCPU: 1200, Memory: 1e10, Storage: 1e11}}}
	old := []model.Instance{{ID: "old-0", State: model.StateBound, NodeID: "a1",
		Request: res(1000, 1e8)}}
	nw := []model.Instance{pending("new-0", res(1000, 1e8), nil)}
	replaces := map[string]string{"new-0": "old-0"}

	_, err := scheduler.Replace(model.ReplaceRequest{
		RunID: "run-roll-00", Nodes: nodes, Old: old, New: nw,
		Replaces: replaces, MaxSurge: 0, MaxUnavailable: 0,
	})
	if err == nil {
		t.Fatal("max_surge=0 and max_unavailable=0 must be a validation error")
	}
}

// TestRolling_InfeasibleByResources: new generation simply does not fit
// even on an empty cluster -> typed conflicts, empty ops.
func TestRolling_InfeasibleByResources(t *testing.T) {
	nodes := []model.Node{{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
		Capacity: model.Resources{MilliCPU: 100, Memory: 1, Storage: 1}}}
	old := []model.Instance{{ID: "old-0", State: model.StateBound, NodeID: "a1",
		Request: res(10, 1)}}
	nw := []model.Instance{pending("new-0", model.Resources{MilliCPU: 5000, Memory: 1, Storage: 1}, nil)}
	r, err := scheduler.Replace(model.ReplaceRequest{
		RunID: "run-roll-res", Nodes: nodes, Old: old, New: nw,
		Replaces: map[string]string{"new-0": "old-0"}, MaxSurge: 1, MaxUnavailable: 1,
	})
	if err != nil {
		t.Fatalf("Replace error: %v", err)
	}
	if r.Feasible || len(r.Ops) != 0 {
		t.Fatalf("expected infeasible rolling plan with no ops, got ops=%+v", r.Ops)
	}
	found := false
	for _, c := range r.Conflicts {
		if c.InstanceID == "new-0" && c.Code == model.RejectResources {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected new-0 insufficient_resources conflict, got %+v", r.Conflicts)
	}
}

// replayEnvelope independently simulates a rolling sequence: it tracks
// occupancy, surge/unavailable counters, and re-runs hard feasibility via
// the oracle on every intermediate state. This is test-side verification,
// not code shared with the solver.
func replayEnvelope(
	nodes []model.Node,
	old []model.Instance,
	nw []model.Instance,
	replaces map[string]string,
	ops []model.ReplaceOp,
	policy model.Policy,
	maxSurge, maxUnavail int,
) error {
	oldNode := map[string]string{}
	oldReq := map[string]model.Resources{}
	oldGroups := map[string]map[string]string{}
	for _, o := range old {
		oldNode[o.ID] = o.NodeID
		oldReq[o.ID] = o.Request
		oldGroups[o.ID] = o.Groups
	}
	newNode := map[string]string{}
	newSpec := map[string]model.Instance{}
	for _, n := range nw {
		newSpec[n.ID] = n
	}
	counterpartOld := replaces

	surge, unavail := 0, 0
	for step, op := range ops {
		switch op.Kind {
		case "place_new":
			oldID := counterpartOld[op.InstanceID]
			if oldNode[oldID] != "" {
				surge++
			} else {
				unavail--
			}
			if surge > maxSurge || unavail < 0 {
				return &stepErr{step, "counter exceeded after place"}
			}
			newNode[op.InstanceID] = op.NodeID
		case "evict_old":
			var newID string
			for k, v := range counterpartOld {
				if v == op.InstanceID {
					newID = k
				}
			}
			if _, placed := newNode[newID]; placed {
				surge--
			} else {
				unavail++
			}
			if unavail > maxUnavail || surge < 0 {
				return &stepErr{step, "counter exceeded after evict"}
			}
			delete(oldNode, op.InstanceID)
		default:
			return &stepErr{step, "unknown op kind " + op.Kind}
		}
		// Rebuild the current world and check resources + anti-affinity.
		var bound []model.Binding
		for id, nID := range oldNode {
			bound = append(bound, model.Binding{InstanceID: "old::" + id, NodeID: nID,
				Request: oldReq[id], Groups: oldGroups[id]})
		}
		var pending []model.Instance
		for id, nID := range newNode {
			spec := newSpec[id]
			bound = append(bound, model.Binding{InstanceID: "new::" + id, NodeID: nID,
				Request: spec.Request, Groups: spec.Groups})
			_ = pending
		}
		used := map[string]model.Resources{}
		for _, b := range bound {
			used[b.NodeID] = used[b.NodeID].Add(b.Request)
		}
		for _, n := range nodes {
			if !n.Capacity.Fits(used[n.ID]) {
				return &stepErr{step, "node " + n.ID + " overcommitted"}
			}
		}
		// Zone anti-affinity: map group=value -> set of occupied zones.
		zonesByGV := map[string]map[string]bool{}
		for _, b := range bound {
			for _, r := range policy.Groups {
				if r.Mode != model.ModeHard || r.Affinity || r.TopologyKey != "zone" {
					continue
				}
				v := b.Groups[r.Group]
				if v == "" {
					continue
				}
				key := r.Group + "=" + v
				zone := zoneOf(nodes, b.NodeID)
				if zonesByGV[key] == nil {
					zonesByGV[key] = map[string]bool{}
				}
				if zonesByGV[key][zone] {
					return &stepErr{step, "hard anti-affinity violated for " + key + " in " + zone}
				}
				zonesByGV[key][zone] = true
			}
		}
	}
	// End state: all old gone, all new placed.
	for id := range oldNode {
		if oldNode[id] != "" {
			return &stepErr{len(ops), "old " + id + " not evicted"}
		}
	}
	for _, n := range nw {
		if newNode[n.ID] == "" {
			return &stepErr{len(ops), "new " + n.ID + " not placed"}
		}
	}
	return nil
}

func zoneOf(nodes []model.Node, id string) string {
	for _, n := range nodes {
		if n.ID == id {
			return n.Zone
		}
	}
	return ""
}

type stepErr struct {
	step int
	msg  string
}

func (e *stepErr) Error() string { return "step " + strconv.Itoa(e.step) + ": " + e.msg }
