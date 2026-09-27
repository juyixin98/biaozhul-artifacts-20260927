package ring_test

import (
	"testing"

	"flowrouter/internal/apperr"
	"flowrouter/internal/ring"
)

func TestAllocateLinearExact(t *testing.T) {
	ms := []ring.Member{{ID: "a", Weight: 1}, {ID: "b", Weight: 2}, {ID: "z", Weight: 0}}
	a, err := ring.AllocateVNodes(ms, 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	if a.Strategy != "linear_weight" {
		t.Fatalf("strategy=%s", a.Strategy)
	}
	if a.Counts["a"] != 100 || a.Counts["b"] != 200 || a.Total != 300 {
		t.Fatalf("counts=%v total=%d", a.Counts, a.Total)
	}
	if _, present := a.Counts["z"]; present {
		t.Fatal("zero-weight member must be absent from counts")
	}
}

func TestAllocateEmpty(t *testing.T) {
	a, err := ring.AllocateVNodes(nil, 10, 0)
	if err != nil {
		t.Fatal(err)
	}
	if a.Strategy != "empty" || a.Total != 0 {
		t.Fatalf("strategy=%s total=%d", a.Strategy, a.Total)
	}
	// all-zero weights produce an empty allocation too
	a2, err := ring.AllocateVNodes([]ring.Member{{ID: "z", Weight: 0}}, 10, 0)
	if err != nil {
		t.Fatal(err)
	}
	if a2.Strategy != "empty" || a2.Total != 0 {
		t.Fatalf("all-zero: strategy=%s total=%d", a2.Strategy, a2.Total)
	}
}

func TestAllocateCappedLargestRemainder(t *testing.T) {
	// cap=10 over weights 1,1,2 -> base 1 each, remBudget=7:
	// extra exact: 7*1/4=1.75 (a), 1.75 (b), 3.5 (c)
	// floors 1,1,3 sum 5, leftover 2 -> remainders .75,.75,.5 -> a,b each +1
	// final: a=3, b=3, c=4
	ms := []ring.Member{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "c", Weight: 2}}
	a, err := ring.AllocateVNodes(ms, 160, 10)
	if err != nil {
		t.Fatal(err)
	}
	if a.Strategy != "largest_remainder" || a.Total != 10 {
		t.Fatalf("strategy=%s total=%d", a.Strategy, a.Total)
	}
	want := map[string]int{"a": 3, "b": 3, "c": 4}
	for id, c := range want {
		if a.Counts[id] != c {
			t.Errorf("%s: got %d want %d (base=%d extra=%d)",
				id, a.Counts[id], c, a.Base[id], a.Extra[id])
		}
	}
	if a.Base["a"] != 1 || a.Extra["a"] != 2 {
		t.Errorf("audit fields wrong: base=%v extra=%v", a.Base, a.Extra)
	}
}

func TestAllocateCappedTieBreakDeterministic(t *testing.T) {
	// Equal weights and cap == member count: everyone gets exactly 1, no
	// remainder budget, fully deterministic.
	var ms []ring.Member
	for _, id := range []string{"a", "b", "c", "d"} {
		ms = append(ms, ring.Member{ID: id, Weight: 1})
	}
	a, err := ring.AllocateVNodes(ms, 160, 4)
	if err != nil {
		t.Fatal(err)
	}
	if a.Total != 4 {
		t.Fatalf("total=%d", a.Total)
	}
	for id := range a.Counts {
		if a.Counts[id] != 1 {
			t.Fatalf("%s got %d vnodes, want exactly 1", id, a.Counts[id])
		}
	}

	// cap=6 equal weights 4 members: base 1 each (4), remBudget 2, all
	// remainders equal 0.5 -> ascending ID tie-break: a and b get the extra.
	a2, err := ring.AllocateVNodes(ms, 160, 6)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]int{"a": 2, "b": 2, "c": 1, "d": 1}
	for id, c := range want {
		if a2.Counts[id] != c {
			t.Errorf("%s: got %d want %d", id, a2.Counts[id], c)
		}
	}
}

func TestAllocateCapErrors(t *testing.T) {
	if _, err := ring.AllocateVNodes(nil, 0, 0); err == nil {
		t.Fatal("vpw < 1 must fail")
	}
	ms := []ring.Member{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}}
	_, err := ring.AllocateVNodes(ms, 10, 1)
	if ae, ok := apperr.As(err); !ok || ae.Code != "CAP_BELOW_MEMBER_COUNT" {
		t.Fatalf("err=%v, want CAP_BELOW_MEMBER_COUNT", err)
	}
}

// TestCappedSkewedWeightFeasible covers the case that motivated the two-stage
// design: weights 1,1,100 with cap=3. A naive floor-then-bump-to-1 overflows
// the cap; the two-stage allocation must stay feasible (1 each).
func TestCappedSkewedWeightFeasible(t *testing.T) {
	ms := []ring.Member{{ID: "a", Weight: 1}, {ID: "b", Weight: 1}, {ID: "c", Weight: 100}}
	a, err := ring.AllocateVNodes(ms, 160, 3)
	if err != nil {
		t.Fatal(err)
	}
	if a.Total != 3 {
		t.Fatalf("total=%d, must equal cap", a.Total)
	}
	for _, id := range []string{"a", "b", "c"} {
		if a.Counts[id] != 1 {
			t.Errorf("%s got %d, want guaranteed 1", id, a.Counts[id])
		}
	}
}

// TestAllocateTotalsConsistent asserts counts sum to total for a matrix of
// weights and caps, which is the rounding invariant operators depend on.
func TestAllocateTotalsConsistent(t *testing.T) {
	weights := [][]int{
		{1, 1, 1}, {1, 2, 3}, {1, 1, 100}, {5, 5, 5, 5}, {1},
	}
	caps := []int{0, 1, 3, 4, 5, 10, 100}
	for _, w := range weights {
		for _, capv := range caps {
			var ms []ring.Member
			for i, x := range w {
				ms = append(ms, ring.Member{ID: string(rune('a' + i)), Weight: x})
			}
			n := 0
			for _, x := range w {
				if x > 0 {
					n++
				}
			}
			if capv > 0 && capv < n {
				continue // rejected by design
			}
			a, err := ring.AllocateVNodes(ms, 13, capv)
			if err != nil {
				t.Fatalf("weights=%v cap=%d: %v", w, capv, err)
			}
			sum := 0
			for _, c := range a.Counts {
				sum += c
			}
			if sum != a.Total {
				t.Fatalf("weights=%v cap=%d sum %d != total %d", w, capv, sum, a.Total)
			}
			if capv > 0 && a.Total != capv {
				t.Fatalf("weights=%v capped total %d != cap %d", w, a.Total, capv)
			}
			if capv == 0 {
				for _, m := range ms {
					if m.Weight > 0 && a.Counts[m.ID] < 1 {
						t.Fatalf("uncapped positive member %s got 0 vnodes", m.ID)
					}
				}
			}
		}
	}
}
