package engine

import (
	"testing"

	"pathvector/internal/model"
)

// fakeIdx is a hand-written indexView for comparator tests; its values are
// authored in the test, never produced by the code under test.
type fakeIdx struct {
	asns  map[string]int
	costs map[string]int
	ord   map[string]int
}

func (f fakeIdx) ASN(id string) int       { return f.asns[id] }
func (f fakeIdx) SameAS(a, b string) bool { return f.asns[a] == f.asns[b] }
func (f fakeIdx) IGPCost(id string) int   { return f.costs[id] }
func (f fakeIdx) Ordinal(id string) int   { return f.ord[id] }

func u32p(v uint32) *uint32 { return &v }

func cand(peer string, lp uint32, path []int, med uint32, o model.Origin, ibgp bool, nh string) *model.Candidate {
	return &model.Candidate{
		Prefix:   "203.0.113.0/24",
		FromPeer: peer,
		NextHop:  nh,
		Attrs: model.Attrs{
			LocalPref:   lp,
			ASPath:      path,
			MED:         med,
			Origin:      o,
			LearnedIBGP: ibgp,
		},
	}
}

// TestCompareOrder asserts EVERY supported comparison step independently:
// a route that differs only in the attribute under test, with every
// higher-precedence attribute identical, must be decided by exactly that
// step.
func TestCompareOrder(t *testing.T) {
	idx := fakeIdx{
		asns:  map[string]int{"ebgp1": 65010, "ebgp2": 65020, "ibgp1": 65000, "tie1": 65010, "tie2": 65010, "rx": 65000},
		costs: map[string]int{"ebgp1": 30, "ebgp2": 10, "ibgp1": 5, "tie1": 7, "tie2": 7, "rx": 1},
		ord:   map[string]int{"ebgp1": 1, "ebgp2": 2, "ibgp1": 3, "tie1": 5, "tie2": 6, "rx": 4},
	}
	ctx := cmpCtx{idx: idx, receiver: "rx"}

	type tc struct {
		name     string
		a, b     *model.Candidate
		wantPeer string // expected winning peer ("" = a's local origin; "tie" = none)
		wantStep string
	}
	ebgp1 := cand("ebgp1", 100, []int{65010}, 0, model.OriginIGP, false, "2.2.2.2")
	ebgp2 := cand("ebgp2", 100, []int{65010}, 0, model.OriginEGP, false, "3.3.3.3")

	cases := []tc{
		{
			name:     "1 local_pref higher wins regardless of path length",
			a:        cand("ebgp1", 200, []int{65010, 65099}, 0, model.OriginIGP, false, ""),
			b:        cand("ebgp2", 100, []int{65010}, 0, model.OriginIGP, false, ""),
			wantPeer: "ebgp1", wantStep: "local_pref",
		},
		{
			name:     "2 shorter as_path wins",
			a:        cand("ebgp1", 100, []int{65010}, 0, model.OriginIGP, false, ""),
			b:        cand("ebgp2", 100, []int{65010, 65099}, 0, model.OriginIGP, false, ""),
			wantPeer: "ebgp1", wantStep: "as_path_length",
		},
		{
			name:     "3 origin igp beats egp",
			a:        ebgp1,
			b:        ebgp2,
			wantPeer: "ebgp1", wantStep: "origin",
		},
		{
			name:     "4 med lower wins when neighboring AS identical",
			a:        cand("ebgp1", 100, []int{65010}, 50, model.OriginIGP, false, ""),
			b:        cand("ebgp2", 100, []int{65010}, 500, model.OriginIGP, false, ""),
			wantPeer: "ebgp1", wantStep: "med",
		},
		{
			name: "4b med ignored across different neighboring AS, decided later by cost",
			a:    cand("ebgp1", 100, []int{65010}, 999, model.OriginIGP, false, ""),
			b:    cand("ebgp2", 100, []int{65020}, 1, model.OriginIGP, false, ""),
			// same localpref/pathlen/origin; med incomparable; source equal
			// (both eBGP); igp cost: ebgp1=30, ebgp2=10 -> b wins.
			wantPeer: "ebgp2", wantStep: "igp_cost",
		},
		{
			name: "5 local origin beats ebgp",
			// Local-origin synthetic candidates may carry a seed AS_PATH;
			// give both routes an identical non-empty path so that
			// source_type (not path length) is the decisive step.
			a:        cand("", 100, []int{65099}, 0, model.OriginIGP, false, "1.1.1.1"),
			b:        cand("ebgp1", 100, []int{65099}, 0, model.OriginIGP, false, "2.2.2.2"),
			wantPeer: "", wantStep: "source_type",
		},
		{
			name:     "5b ebgp beats ibgp learned",
			a:        cand("ebgp1", 100, []int{65010}, 0, model.OriginIGP, false, ""),
			b:        cand("ibgp1", 100, []int{65010}, 0, model.OriginIGP, true, ""),
			wantPeer: "ebgp1", wantStep: "source_type",
		},
		{
			name:     "6 lower igp cost to egress wins",
			a:        cand("ebgp1", 100, []int{65010}, 0, model.OriginIGP, false, ""),
			b:        cand("ebgp2", 100, []int{65020}, 0, model.OriginIGP, false, ""),
			wantPeer: "ebgp2", wantStep: "igp_cost",
		},
		{
			name: "7 router id ordinal is final deterministic tie-break",
			// Same neighboring AS (MED comparable and equal), equal IGP
			// cost; only the declaration ordinal remains.
			a:        cand("tie2", 100, []int{65010}, 0, model.OriginIGP, false, ""),
			b:        cand("tie1", 100, []int{65010}, 0, model.OriginIGP, false, ""),
			wantPeer: "tie1", wantStep: "router_id",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w, step := CompareTwo(tc.a, tc.b, ctx)
			if step != tc.wantStep {
				t.Fatalf("decisive step = %q, want %q", step, tc.wantStep)
			}
			gotPeer := "<nil>"
			if w != nil {
				gotPeer = w.FromPeer
			}
			if gotPeer != tc.wantPeer {
				t.Fatalf("winner peer = %q, want %q", gotPeer, tc.wantPeer)
			}
		})
	}
}

// TestCompareTie reports an exact tie when every attribute matches and the
// peers are the same identity (no distinct peer can fully tie).
func TestCompareTie(t *testing.T) {
	idx := fakeIdx{asns: map[string]int{"x": 1, "y": 2}, costs: map[string]int{"x": 1}, ord: map[string]int{"x": 0}}
	ctx := cmpCtx{idx: idx, receiver: "x"}
	a := cand("x", 100, nil, 0, model.OriginIGP, false, "")
	b := cand("x", 100, nil, 0, model.OriginIGP, false, "")
	w, step := CompareTwo(a, b, ctx)
	if w != nil || step != "tie" {
		t.Fatalf("got winner=%v step=%q, want tie", w, step)
	}
}

// TestContainsAS guards the AS_PATH loop predicate directly.
func TestContainsAS(t *testing.T) {
	a := model.Attrs{ASPath: []int{65001, 65020, 65001}}
	if !a.ContainsAS(65020) {
		t.Fatal("65020 present but ContainsAS=false")
	}
	if a.ContainsAS(65099) {
		t.Fatal("65099 absent but ContainsAS=true")
	}
}
