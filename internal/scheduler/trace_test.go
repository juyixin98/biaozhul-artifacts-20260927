package scheduler_test

import (
	"testing"

	"placer/internal/model"
	"placer/internal/scheduler"
)

// TestTrace_GreedyReasonAuditable forces the greedy path (> exact threshold)
// and asserts each pick records per-node scores and a skew snapshot, so the
// reason for every choice is auditable rather than asserted only by the
// core itself.
func TestTrace_GreedyReasonAuditable(t *testing.T) {
	// 3 zones, 13 tiny instances -> exceeds default exact threshold of 12.
	var nodes []model.Node
	for _, z := range []string{"za", "zb", "zc"} {
		nodes = append(nodes, model.Node{ID: "n-" + z, Zone: z, Region: "r",
			Status:   model.NodeReady,
			Capacity: model.Resources{MilliCPU: 100000, Memory: 1e12, Storage: 1e12}})
	}
	var inst []model.Instance
	for k := 0; k < 13; k++ {
		inst = append(inst, pending(
			"g"+pad2(k), model.Resources{MilliCPU: 100, Memory: 1, Storage: 1}, nil))
	}
	r := mustPlan(t, model.PlanRequest{RunID: "run-greedy-trace", Nodes: nodes, Instances: inst})
	if !r.Feasible {
		t.Fatalf("expected feasible: %+v", r.Conflicts)
	}
	if r.Solver != "greedy" {
		t.Fatalf("13 instances must select greedy solver, got %q", r.Solver)
	}
	picks := 0
	for _, st := range r.Trace {
		if st.Kind != "greedy_pick" {
			continue
		}
		picks++
		if st.NodeID == "" || st.InstanceID == "" {
			t.Fatal("greedy_pick missing instance/node ids")
		}
		if st.Skew == nil {
			t.Fatalf("pick for %s missing skew snapshot", st.InstanceID)
		}
		// The chosen node must be present in the score table and must be a
		// strict-or-tie minimum among the recorded scores.
		winner, ok := st.NodeScores[st.NodeID]
		if !ok {
			t.Fatalf("chosen node %s not in its own score table", st.NodeID)
		}
		for nID, sc := range st.NodeScores {
			if nodeScoreLess(sc, winner) {
				t.Fatalf("instance %s: chosen %s (%+v) but %s scores better (%+v)",
					st.InstanceID, st.NodeID, winner, nID, sc)
			}
		}
	}
	if picks != 13 {
		t.Fatalf("expected 13 audited picks, got %d", picks)
	}

	// Independently verify the greedy result balances perfectly: 13 across
	// 3 zones => counts {5,4,4}, skew 1.
	counts := map[string]int{}
	for _, d := range r.Decisions {
		counts[zoneOf(nodes, d.NodeID)]++
	}
	lo, hi := 13, 0
	for _, c := range counts {
		if c < lo {
			lo = c
		}
		if c > hi {
			hi = c
		}
	}
	if hi-lo != 1 {
		t.Fatalf("expected balanced counts {5,4,4}, got %+v (skew %d)", counts, hi-lo)
	}
}

// TestSoftRuleNeverOverridesHard proves soft anti-affinity only affects
// ranking. Two equal-skew choices exist; the soft rule breaks the tie away
// from the occupied zone, but it never makes an illegal node legal.
func TestSoftRuleNeverOverridesHard(t *testing.T) {
	// 2 zones, one node each; a bound web in za; two pending: one web with
	// a hard zone pin to za (legal), one web unpinned. Soft anti-affinity
	// must push the unpinned one to zb; hard pin keeps the other in za.
	nodes := []model.Node{
		{ID: "a1", Zone: "za", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
		{ID: "b1", Zone: "zb", Region: "r", Status: model.NodeReady,
			Capacity: model.Resources{MilliCPU: 8000, Memory: 1e10, Storage: 1e11}},
	}
	bound := []model.Binding{{InstanceID: "b0", NodeID: "a1",
		Request: res(100, 1), Groups: map[string]string{"app": "web"}}}
	pinned := pending("pinned", res(100, 1), map[string]string{"app": "web"})
	pinned.Zone = "za"
	free := pending("free", res(100, 1), map[string]string{"app": "web"})
	policy := model.Policy{Groups: []model.GroupRule{
		{Group: "app", Mode: model.ModeSoft, TopologyKey: "zone"},
	}}
	r := mustPlan(t, model.PlanRequest{
		RunID: "run-soft", Nodes: nodes, Instances: []model.Instance{pinned, free},
		Bound: bound, Policy: policy,
	})
	if !r.Feasible {
		t.Fatalf("soft rule cannot make a feasible request infeasible: %+v", r.Conflicts)
	}
	if got := findDecision(t, r, "pinned"); got != "a1" {
		t.Fatalf("hard zone pin must hold despite soft anti-affinity, got %q", got)
	}
	if got := findDecision(t, r, "free"); got != "b1" {
		t.Fatalf("soft anti-affinity should move free instance to zb, got %q", got)
	}
}

// TestSearchBudgetIsErrorNotConflict guarantees budget exhaustion is
// reported as an error, never disguised as a conflict or a success.
func TestSearchBudgetIsErrorNotConflict(t *testing.T) {
	// Build an instance set large enough to force exact enumeration with a
	// deliberately tiny budget.
	var nodes []model.Node
	for k := 0; k < 6; k++ {
		nodes = append(nodes, model.Node{ID: "n" + pad2(k), Zone: "z" + pad2(k%3), Region: "r",
			Status:   model.NodeReady,
			Capacity: model.Resources{MilliCPU: 1e6, Memory: 1e12, Storage: 1e12}})
	}
	var inst []model.Instance
	for k := 0; k < 8; k++ {
		inst = append(inst, pending("i"+pad2(k), res(10, 1), nil))
	}
	_, err := scheduler.Plan(model.PlanRequest{
		RunID: "run-budget", Nodes: nodes, Instances: inst,
		Options: model.PlanOptions{MaxNodesForExact: 8, SearchBudget: 2},
	})
	if err == nil {
		t.Fatal("tiny budget must surface a search-exhausted error")
	}
	var exhausted *scheduler.SearchExhausted
	if !asSearchExhausted(err, &exhausted) {
		t.Fatalf("expected *SearchExhausted, got %T %v", err, err)
	}
	if exhausted.Visits <= exhausted.Budget {
		t.Fatalf("exhaustion accounting wrong: %+v", exhausted)
	}
}

func asSearchExhausted(err error, target **scheduler.SearchExhausted) bool {
	for err != nil {
		if se, ok := err.(*scheduler.SearchExhausted); ok {
			*target = se
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

func nodeScoreLess(a, b model.NodeScore) bool {
	if a.Skew != b.Skew {
		return a.Skew < b.Skew
	}
	if a.SumSquares != b.SumSquares {
		return a.SumSquares < b.SumSquares
	}
	return a.SoftGroups < b.SoftGroups
}

func pad2(k int) string {
	if k < 10 {
		return "0" + string(rune('0'+k))
	}
	return string(rune('0'+k/10)) + string(rune('0'+k%10))
}
