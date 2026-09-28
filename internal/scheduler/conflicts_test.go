package scheduler_test

import (
	"testing"

	"placer/internal/model"
	"placer/internal/scheduler"
)

// TestConflict_MutualAntiAffinity: two instances in the same hard
// anti-affinity group with only one usable zone. Each alone is legal;
// together they are mutually exclusive. The result must be infeasible and
// both conflicts must carry the anti_affinity category and name each other.
func TestConflict_MutualAntiAffinity(t *testing.T) {
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
		{ID: "a2", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
		{ID: "b1", Zone: "zb", Region: "r", Status: model.NodeDisabled,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
	}
	instances := []model.Instance{
		pending("i0", res(100, 1e8), map[string]string{"app": "w"}),
		pending("i1", res(100, 1e8), map[string]string{"app": "w"}),
	}
	policy := model.Policy{Groups: []model.GroupRule{
		{Group: "app", Mode: model.ModeHard, Affinity: false, TopologyKey: "zone"},
	}}

	r := mustPlan(t, model.PlanRequest{
		RunID: "run-aa-mutual", Nodes: nodes, Instances: instances, Policy: policy,
	})
	if r.Feasible {
		t.Fatalf("expected infeasible, got decisions %+v", r.Decisions)
	}
	if len(r.Decisions) != 0 {
		t.Fatalf("conflict must return NO decisions, got %d", len(r.Decisions))
	}
	codes := conflictCodes(r)
	for _, id := range []string{"i0", "i1"} {
		if codes[id] != model.RejectAntiAffinity {
			t.Fatalf("instance %s expected anti_affinity_conflict, got %q (all: %+v)",
				id, codes[id], r.Conflicts)
		}
	}
	byID := map[string]model.Conflict{}
	for _, c := range r.Conflicts {
		byID[c.InstanceID] = c
	}
	if !contains(byID["i0"].BlockedBy, "i1") || !contains(byID["i1"].BlockedBy, "i0") {
		t.Fatalf("conflicts must name the blocking peer, got %+v %+v",
			byID["i0"].BlockedBy, byID["i1"].BlockedBy)
	}
}

// TestConflict_InsufficientResources: one ready node, an instance asking
// for more than its capacity in every dimension variant. The conflict
// category must be insufficient_resources and no other node may be chosen.
func TestConflict_InsufficientResources(t *testing.T) {
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 1000, Memory: 2e9, Storage: 5e10}},
	}
	cases := []struct {
		name string
		req  model.Resources
	}{
		{"cpu", model.Resources{MilliCPU: 2000, Memory: 1, Storage: 1}},
		{"memory", model.Resources{MilliCPU: 1, Memory: 5e9, Storage: 1}},
		{"storage", model.Resources{MilliCPU: 1, Memory: 1, Storage: 9e10}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			instances := []model.Instance{pending("hungry-"+tc.name, tc.req, nil)}
			r := mustPlan(t, model.PlanRequest{
				RunID: "run-res-" + tc.name, Nodes: nodes, Instances: instances,
			})
			if r.Feasible {
				t.Fatal("oversized instance must not be placed")
			}
			codes := conflictCodes(r)
			if codes["hungry-"+tc.name] != model.RejectResources {
				t.Fatalf("expected insufficient_resources, got %q: %+v",
					codes["hungry-"+tc.name], r.Conflicts)
			}
		})
	}
}

// TestConflict_SimultaneousReservation verifies rule 3 with the classic
// trap: two large instances, each fitting only alone on a node that two
// nodes share capacity for collectively only if spread. A naive per-instance
// independent placement could double-book; temporary reservations must
// prevent it.
func TestConflict_SimultaneousReservation(t *testing.T) {
	// Two nodes in one zone, each 1000 cpu. Two instances of 700: only one
	// fits per node; there are exactly two nodes => feasible on different
	// nodes. Then add a third 700 instance => infeasible by resources.
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 1000, Memory: 1e10, Storage: 1e11}},
		{ID: "a2", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 1000, Memory: 1e10, Storage: 1e11}},
	}
	mk := func(ids ...string) []model.Instance {
		var out []model.Instance
		for _, id := range ids {
			out = append(out, pending(id, model.Resources{MilliCPU: 700, Memory: 1, Storage: 1}, nil))
		}
		return out
	}

	ok := mustPlan(t, model.PlanRequest{RunID: "run-sim-ok", Nodes: nodes, Instances: mk("i0", "i1")})
	if !ok.Feasible {
		t.Fatalf("two 700 instances should spread one-per-node, conflicts=%+v", ok.Conflicts)
	}
	m := map[string]int{}
	for _, d := range ok.Decisions {
		m[d.NodeID]++
	}
	if m["a1"] != 1 || m["a2"] != 1 {
		t.Fatalf("expected one per node, got %+v", m)
	}

	bad := mustPlan(t, model.PlanRequest{RunID: "run-sim-bad", Nodes: nodes, Instances: mk("i0", "i1", "i2")})
	if bad.Feasible {
		t.Fatalf("three 700 instances cannot fit two 1000-capacity nodes, got %+v", bad.Decisions)
	}
	codes := conflictCodes(bad)
	if codes["i2"] != model.RejectResources {
		t.Fatalf("the crowded-out instance should report insufficient_resources, got %q (%+v)",
			codes["i2"], bad.Conflicts)
	}
}

// TestConflict_DomainMissing verifies rule 2's missing-domain case: a hard
// group rule on topology key "zone" makes a node with an empty zone
// illegal, and the rejection category is domain_missing — not a silent
// count, and not a mislabeled resource failure.
func TestConflict_DomainMissing(t *testing.T) {
	nodes := []model.Node{
		{ID: "a1", Zone: "", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
		{ID: "b1", Zone: "zb", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
	}
	instances := []model.Instance{pending("i0", res(100, 1e8), map[string]string{"app": "w"})}
	policy := model.Policy{Groups: []model.GroupRule{
		{Group: "app", Mode: model.ModeHard, TopologyKey: "zone"},
	}}
	r := mustPlan(t, model.PlanRequest{
		RunID: "run-domain-missing", Nodes: nodes, Instances: instances, Policy: policy,
	})
	if !r.Feasible {
		t.Fatal("instance should still be legal on b1 which has a zone")
	}
	if got := findDecision(t, r, "i0"); got != "b1" {
		t.Fatalf("zoneless a1 must be filtered; expected b1, got %q", got)
	}
	// Now remove the good node: the only candidate is the zoneless one.
	nodesOnlyBad := []model.Node{nodes[0]}
	only := mustPlan(t, model.PlanRequest{
		RunID: "run-domain-missing-only", Nodes: nodesOnlyBad, Instances: instances, Policy: policy,
	})
	if only.Feasible {
		t.Fatal("zoneless node cannot satisfy a hard zone-domain rule")
	}
	codes := conflictCodes(only)
	if codes["i0"] != model.RejectDomainMissing {
		t.Fatalf("expected domain_missing, got %q (%+v)", codes["i0"], only.Conflicts)
	}
}

// TestConflict_ZoneAndSelectorAndStatus pins the other intrinsic categories.
func TestConflict_ZoneAndSelectorAndStatus(t *testing.T) {
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11},
			Labels:   map[string]string{"disk": "hdd"}},
		{ID: "a2", Zone: "za", Region: "r", Status: model.NodeNotReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
	}
	t.Run("zone_mismatch", func(t *testing.T) {
		in := pending("z", res(100, 1), nil)
		in.Zone = "zz"
		r := mustPlan(t, model.PlanRequest{RunID: "run-zone", Nodes: nodes, Instances: []model.Instance{in}})
		if r.Feasible || conflictCodes(r)["z"] != model.RejectZoneMismatch {
			t.Fatalf("expected zone_mismatch, got feasible=%v conflicts=%+v", r.Feasible, r.Conflicts)
		}
	})
	t.Run("selector_unmatched", func(t *testing.T) {
		in := pending("s", res(100, 1), nil)
		in.NodeSelector = map[string]string{"disk": "ssd"}
		r := mustPlan(t, model.PlanRequest{RunID: "run-sel", Nodes: nodes, Instances: []model.Instance{in}})
		if r.Feasible || conflictCodes(r)["s"] != model.RejectNodeSelector {
			t.Fatalf("expected node_selector_unmatched, got feasible=%v conflicts=%+v", r.Feasible, r.Conflicts)
		}
	})
	t.Run("all_nodes_not_ready", func(t *testing.T) {
		down := []model.Node{nodes[1]} // only the not_ready node
		r := mustPlan(t, model.PlanRequest{RunID: "run-down", Nodes: down,
			Instances: []model.Instance{pending("d", res(100, 1), nil)}})
		if r.Feasible || conflictCodes(r)["d"] != model.RejectNodeNotReady {
			t.Fatalf("expected node_not_ready, got feasible=%v conflicts=%+v", r.Feasible, r.Conflicts)
		}
	})
}

// TestNoSolutionDoesNotPlaceAnything is the explicit rule-4 guarantee:
// infeasible requests never return partial/arbitrary decisions.
func TestNoSolutionDoesNotPlaceAnything(t *testing.T) {
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 500, Memory: 1e10, Storage: 1e11}},
	}
	instances := []model.Instance{
		pending("ok", res(100, 1), nil),
		pending("toobig", model.Resources{MilliCPU: 5000, Memory: 1, Storage: 1}, nil),
	}
	r := mustPlan(t, model.PlanRequest{RunID: "run-atomic", Nodes: nodes, Instances: instances})
	if r.Feasible {
		t.Fatal("batch must be infeasible")
	}
	if len(r.Decisions) != 0 {
		t.Fatalf("even the feasible instance must not be partially placed, got %+v", r.Decisions)
	}
}

// TestValidationErrorIsNotAConflict ensures bad input is a typed error,
// never downgraded into a "no solution" conflict or a success.
func TestValidationErrorIsNotAConflict(t *testing.T) {
	if _, err := scheduler.Plan(model.PlanRequest{RunID: "run-bad"}); err == nil {
		t.Fatal("empty nodes/instances must return a validation error")
	}
}
