package scheduler_test

import (
	"strings"
	"testing"

	"opp284/placement/internal/fixture"
	"opp284/placement/internal/model"
	"opp284/placement/internal/scheduler"
)

func limits() scheduler.SearchLimits {
	return scheduler.SearchLimits{MaxInstances: 8, MaxCandidates: 12, MaxLeafVisits: 200000}
}

// toSnap converts a fixture cluster to a scheduler snapshot.
func toSnap(c fixture.Cluster) scheduler.Snapshot {
	snap := scheduler.Snapshot{
		Nodes:         c.Nodes,
		DeclaredZones: c.DeclaredZones,
		Groups:        c.Groups,
	}
	for _, r := range c.Running {
		snap.Running = append(snap.Running, scheduler.RunningInstance{
			ID: r.ID, NodeID: r.NodeID, Request: r.Request, AffinityGroups: r.AffinityGroups,
		})
	}
	return snap
}

func toRequest(s *fixture.Scenario) scheduler.Request {
	req := scheduler.Request{
		PlanID:        "test-plan",
		Intents:       s.Intents,
		AllowRecreate: s.AllowRecreate,
	}
	if len(s.Replacements) > 0 {
		req.Replacements = map[string]scheduler.Replacement{}
		for k, v := range s.Replacements {
			req.Replacements[k] = scheduler.Replacement{OldID: v.OldID}
		}
	}
	return req
}

func failureCodeFor(f *model.PlanFailure, instanceID string) model.ReasonKind {
	if f == nil {
		return ""
	}
	for _, in := range f.Instances {
		if in.InstanceID == instanceID {
			if len(in.Reasons) > 0 {
				return in.Reasons[0].Code
			}
		}
	}
	return ""
}

// TestExhaustiveSkewSpread: three small instances with no constraints over
// the canonical 3-zone cluster must spread one-per-zone (oracle-verified).
func TestExhaustiveSkewSpread(t *testing.T) {
	c := fixture.BuildSmallCluster()
	snap := toSnap(c)
	intents := []model.Intent{
		fixture.Instance("i-1", 1, 2, "", model.Selector{}),
		fixture.Instance("i-2", 1, 2, "", model.Selector{}),
		fixture.Instance("i-3", 1, 2, "", model.Selector{}),
	}
	req := scheduler.Request{PlanID: "p", Intents: intents}

	dec, steps, fail := scheduler.Solve(snap, req, limits())
	if fail != nil {
		t.Fatalf("expected success, got %v", fail)
	}
	if !dec.Exhaustive {
		t.Fatalf("expected exhaustive search, got fallback; steps=%d", len(steps))
	}
	if dec.LeafVisits == 0 {
		t.Fatal("leaf count must be > 0 for exhaustive enumeration")
	}
	if dec.Score.ZoneCountRange != 0 {
		t.Fatalf("expected perfectly spread zones (range 0), got range %d score=%+v",
			dec.Score.ZoneCountRange, dec.Score)
	}
	zones := map[string]int{}
	for _, p := range dec.Placements {
		zones[p.Zone]++
	}
	if len(zones) != 3 {
		t.Fatalf("expected placement in 3 distinct zones, got %v", zones)
	}

	// Independent oracle: the implementation's assignment must be feasible
	// per the oracle AND tied for the oracle optimum.
	oc := newOracleCluster(snap, false)
	best, tied := oc.bestAssignment(intents, map[string]bool{}, map[string]string{})
	if best.rng != 0 {
		t.Fatalf("oracle expected range 0, got %d", best.rng)
	}
	chosen := map[string]string{}
	for _, p := range dec.Placements {
		chosen[p.InstanceID] = p.NodeID
	}
	if !assignmentIn(chosen, tied) {
		t.Fatalf("chosen %v not among oracle optimum set (%d assignments)", chosen, len(tied))
	}
}

func assignmentIn(a map[string]string, set []map[string]string) bool {
	for _, b := range set {
		same := len(a) == len(b)
		for k, v := range a {
			if b[k] != v {
				same = false
			}
		}
		if same {
			return true
		}
	}
	return false
}

// TestHardFilterBeforeScore: even though placing a huge instance on cordoned
// n-6 could hypothetically "balance" skew, it must never be selected.
func TestHardFilterBeforeScore(t *testing.T) {
	c := fixture.BuildSmallCluster()
	// Pin to z-c and require a label that only the eligible n-5 lacks matches:
	// require role=web in z-c -> only n-6 matches the label, but n-6 is
	// cordoned => hard failure, not a soft tradeoff.
	intents := []model.Intent{
		fixture.Instance("i-1", 1, 2, "z-c", model.Selector{Equal: map[string]string{"role": "web"}}),
	}
	_, _, fail := scheduler.Solve(toSnap(c), scheduler.Request{PlanID: "p", Intents: intents}, limits())
	if fail == nil {
		t.Fatal("expected failure: only matching node is cordoned")
	}
	if got := failureCodeFor(fail, "i-1"); got != model.ReasonDomainNoEligibleNode {
		t.Fatalf("expected DOMAIN_NO_ELIGIBLE_NODE, got %s (%v)", got, fail)
	}
}

// TestMissingDomain is driven by the authored JSON fixture.
func TestScenarioFixture(t *testing.T) {
	cases := []struct {
		file string
	}{
		{"../../testdata/scenario_missing_domain.json"},
		{"../../testdata/scenario_mutual_antiaffinity.json"},
		{"../../testdata/scenario_insufficient.json"},
		{"../../testdata/scenario_rolling_blocked.json"},
	}
	for _, tc := range cases {
		t.Run(tc.file, func(t *testing.T) {
			s, err := fixture.LoadScenario(tc.file)
			if err != nil {
				t.Fatal(err)
			}
			dec, steps, fail := scheduler.Solve(toSnap(s.Cluster), toRequest(s), limits())
			if s.ExpectFailure == nil {
				t.Fatalf("fixture %s missing expect_failure", tc.file)
			}
			if fail == nil {
				t.Fatalf("expected failure %s, got decision %+v", s.ExpectFailure.TopCode, dec)
			}
			if fail.Code != s.ExpectFailure.TopCode {
				t.Fatalf("top code: want %s got %s", s.ExpectFailure.TopCode, fail.Code)
			}
			for iid, wantCode := range s.ExpectFailure.InstanceCodes {
				got := failureCodeFor(fail, iid)
				if got != wantCode {
					t.Fatalf("instance %s: want %s got %s", iid, wantCode, got)
				}
			}
			// Every failed instance must have concrete per-node reasons in the
			// trace (not only a generic top-level code).
			if !stepsContainReject(steps) {
				t.Fatal("trace contains no concrete hard rejects; failure reason not explained")
			}
		})
	}
}

func stepsContainReject(steps []scheduler.Step) bool {
	for _, st := range steps {
		for _, c := range st.Candidates {
			if len(c.HardRejects) > 0 {
				return true
			}
		}
	}
	return false
}

// TestMutualAntiAffinityMechanism: the joint-only conflict must remain
// invisible at static filter time but be caught by temporary reservations.
func TestMutualAntiAffinityMechanism(t *testing.T) {
	s, err := fixture.LoadScenario("../../testdata/scenario_mutual_antiaffinity.json")
	if err != nil {
		t.Fatal(err)
	}
	// Independent oracle agrees infeasible.
	oc := newOracleCluster(toSnap(s.Cluster), false)
	feasible := oc.feasibleAssignments(s.Intents, map[string]bool{}, map[string]string{})
	if len(feasible) != 0 {
		t.Fatalf("oracle: expected 0 feasible assignments, got %d", len(feasible))
	}
	_, steps, fail := scheduler.Solve(toSnap(s.Cluster), toRequest(s), limits())
	if fail == nil {
		t.Fatal("expected ANTI_AFFINITY conflict")
	}
	// Static filter steps must show n-5 initially accepted for both.
	staticAccepts := 0
	for _, st := range steps {
		if st.Stage == "hard_filter" {
			for _, c := range st.Candidates {
				if c.NodeID == "n-5" && c.Accepted {
					staticAccepts++
				}
			}
		}
	}
	if staticAccepts != 2 {
		t.Fatalf("expected n-5 statically legal for both intents, got %d accepts", staticAccepts)
	}
	// Branch filters must record the mutual reject.
	mutual := false
	for _, st := range steps {
		for _, c := range st.Candidates {
			for _, hr := range c.HardRejects {
				if hr.Code == model.ReasonAntiAffinityConflict {
					mutual = true
					if !strings.Contains(hr.Detail, "g-spread") {
						t.Fatalf("reject detail must name the group, got %q", hr.Detail)
					}
				}
			}
		}
	}
	if !mutual {
		t.Fatal("no ANTI_AFFINITY_CONFLICT reject recorded during branching")
	}
}

// TestRollingSuccess: replacement lands off the old node; replacements map
// records replaces_id.
func TestRollingSuccess(t *testing.T) {
	s, err := fixture.LoadScenario("../../testdata/scenario_rolling_success.json")
	if err != nil {
		t.Fatal(err)
	}
	dec, _, fail := scheduler.Solve(toSnap(s.Cluster), toRequest(s), limits())
	if fail != nil {
		t.Fatalf("expected success, got %v", fail)
	}
	want := s.ExpectPlacement["i-new"]
	got := placementNode(dec, "i-new")
	if got != want {
		t.Fatalf("i-new: want node %s got %s", want, got)
	}
	for _, p := range dec.Placements {
		if p.InstanceID == "i-new" && p.ReplacesID != "r-old" {
			t.Fatalf("replaces_id: want r-old, got %q", p.ReplacesID)
		}
	}
	// Oracle confirms optimality/feasibility.
	oc := newOracleCluster(toSnap(s.Cluster), false)
	blocked := map[string]string{"i-new": "n-1"}
	best, tied := oc.bestAssignment(s.Intents, map[string]bool{}, blocked)
	_ = best
	chosen := map[string]string{}
	for _, p := range dec.Placements {
		chosen[p.InstanceID] = p.NodeID
	}
	if !assignmentIn(chosen, tied) {
		t.Fatalf("rolling placement %v not among oracle optimum", chosen)
	}
}

func placementNode(d *scheduler.Decision, iid string) string {
	for _, p := range d.Placements {
		if p.InstanceID == iid {
			return p.NodeID
		}
	}
	return ""
}

// TestRollingBlockedDoesNotEvict: allow_recreate=false must surface
// ROLLING_REPLACEMENT_BLOCKED; with recreate it must succeed on the old node.
func TestRollingBlockedDoesNotEvict(t *testing.T) {
	s, err := fixture.LoadScenario("../../testdata/scenario_rolling_blocked.json")
	if err != nil {
		t.Fatal(err)
	}
	_, _, fail := scheduler.Solve(toSnap(s.Cluster), toRequest(s), limits())
	if fail == nil || failureCodeFor(fail, "i-new") != model.ReasonRollingBlocked {
		t.Fatalf("want ROLLING_REPLACEMENT_BLOCKED, got %v", fail)
	}

	// Same request with recreate allowed evicts r-old and fits on n-1.
	req := toRequest(s)
	req.AllowRecreate = true
	dec, _, fail2 := scheduler.Solve(toSnap(s.Cluster), req, limits())
	if fail2 != nil {
		t.Fatalf("recreate=true expected success, got %v", fail2)
	}
	if got := placementNode(dec, "i-new"); got != "n-1" {
		t.Fatalf("recreate should reuse old node n-1, got %s", got)
	}
}

// TestSimultaneousReservation: two 8-cpu instances and exactly two nodes that
// fit one each: sequential-but-for-reservation double-booking is impossible.
func TestSimultaneousReservation(t *testing.T) {
	c := fixture.Cluster{
		Name: "two-fit",
		Nodes: []model.Node{
			{ID: "n-1", Zone: "z-a", Capacity: model.Resources{"cpu": 8}, Labels: model.Labels{}, Eligible: true},
			{ID: "n-2", Zone: "z-a", Capacity: model.Resources{"cpu": 8}, Labels: model.Labels{}, Eligible: true},
		},
	}
	intents := []model.Intent{
		fixture.Instance("a", 8, 0, "", model.Selector{}),
		fixture.Instance("b", 8, 0, "", model.Selector{}),
	}
	dec, _, fail := scheduler.Solve(toSnap(c), scheduler.Request{PlanID: "p", Intents: intents}, limits())
	if fail != nil {
		t.Fatalf("expected success on separate nodes, got %v", fail)
	}
	m := map[string]string{}
	for _, p := range dec.Placements {
		m[p.InstanceID] = p.NodeID
	}
	if m["a"] == m["b"] {
		t.Fatalf("both placed on %s despite temporary reservation", m["a"])
	}
}

// TestEmptyAndIneligibleDomainSemantics documents D* explicitly.
func TestEmptyAndIneligibleDomainSemantics(t *testing.T) {
	c := fixture.BuildSmallCluster()
	snap := toSnap(c)
	snap.DeclaredZones = []string{"z-a", "z-b", "z-c", "z-empty"}

	// Default: z-empty is declared but has no nodes -> NOT in D*.
	intents := []model.Intent{fixture.Instance("i", 1, 2, "", model.Selector{})}
	lm := limits()
	lm.IncludeEmptyDZ = false
	dec, _, fail := scheduler.Solve(snap, scheduler.Request{PlanID: "p", Intents: intents}, lm)
	if fail != nil {
		t.Fatal(fail)
	}
	if containsStr(dec.ParticipatingDomains, "z-empty") {
		t.Fatalf("z-empty must be excluded from D* by default, got %v", dec.ParticipatingDomains)
	}
	if len(dec.ParticipatingDomains) != 3 {
		t.Fatalf("D* should contain z-a,z-b,z-c, got %v", dec.ParticipatingDomains)
	}

	// IncludeEmptyDZ: z-empty joins D* as a zero-load domain.
	lm2 := limits()
	lm2.IncludeEmptyDZ = true
	dec2, _, fail := scheduler.Solve(snap, scheduler.Request{PlanID: "p2", Intents: intents}, lm2)
	if fail != nil {
		t.Fatal(fail)
	}
	if !containsStr(dec2.ParticipatingDomains, "z-empty") {
		t.Fatalf("z-empty must be in D* when include_empty_declared, got %v", dec2.ParticipatingDomains)
	}
}

func containsStr(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// TestDeterministicSignature: repeated solves on identical input return the
// identical plan content.
func TestDeterministicSignature(t *testing.T) {
	c := fixture.BuildSmallCluster()
	intents := []model.Intent{
		fixture.Instance("i-1", 1, 2, "", model.Selector{}),
		fixture.Instance("i-2", 1, 2, "", model.Selector{}),
	}
	d1, _, f1 := scheduler.Solve(toSnap(c), scheduler.Request{PlanID: "p", Intents: intents}, limits())
	d2, _, f2 := scheduler.Solve(toSnap(c), scheduler.Request{PlanID: "p", Intents: intents}, limits())
	if f1 != nil || f2 != nil {
		t.Fatal("unexpected failure")
	}
	if d1.Score.Signature != d2.Score.Signature {
		t.Fatalf("nondeterministic: %q vs %q", d1.Score.Signature, d2.Score.Signature)
	}
}

// TestGreedyFallbackRecorded: exceeding caps still returns a feasible answer
// and explicitly records the fallback in the trace.
func TestGreedyFallbackRecorded(t *testing.T) {
	c := fixture.BuildSmallCluster()
	intents := []model.Intent{
		fixture.Instance("i-1", 1, 2, "", model.Selector{}),
		fixture.Instance("i-2", 1, 2, "", model.Selector{}),
	}
	lm := limits()
	lm.MaxInstances = 1 // forces greedy
	dec, steps, fail := scheduler.Solve(toSnap(c), scheduler.Request{PlanID: "p", Intents: intents}, lm)
	if fail != nil {
		t.Fatal(fail)
	}
	if dec.Exhaustive {
		t.Fatal("expected greedy fallback, reported exhaustive")
	}
	found := false
	for _, st := range steps {
		if st.Stage == "search_strategy" && strings.Contains(st.Detail, "greedy") {
			found = true
		}
	}
	if !found {
		t.Fatal("greedy fallback not recorded in trace")
	}
}
