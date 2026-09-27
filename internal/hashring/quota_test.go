package hashring

import (
	"math"
	"strconv"
	"testing"

	"flexhash/internal/fherr"
)

func wm(ids ...string) []memberWeight {
	out := make([]memberWeight, len(ids)/2)
	for i := 0; i < len(ids); i += 2 {
		w, err := strconv.Atoi(ids[i+1])
		if err != nil {
			panic(err)
		}
		out[i/2] = memberWeight{ids[i], w}
	}
	return out
}

func TestAllocateQuotasConcrete(t *testing.T) {
	cases := []struct {
		name    string
		weights []memberWeight
		buckets int
		want    map[string]int
	}{
		{
			name:    "equal 3 on 1024",
			weights: []memberWeight{{"a", 1}, {"b", 1}, {"c", 1}},
			buckets: 1024,
			// exact 341.333: floors 341 each = 1023, last remainder ->
			// a (tie break id ascending).
			want: map[string]int{"a": 342, "b": 341, "c": 341},
		},
		{
			name:    "3:2:1 on 10 buckets",
			weights: []memberWeight{{"a", 3}, {"b", 2}, {"c", 1}},
			buckets: 10,
			// exact: 5.0, 3.333, 1.667 -> floors 5,3,1 = 9; remainder .667 -> c
			want: map[string]int{"a": 5, "b": 3, "c": 2},
		},
		{
			name:    "weights divide exactly",
			weights: []memberWeight{{"a", 1}, {"b", 3}},
			buckets: 8,
			want:    map[string]int{"a": 2, "b": 6},
		},
		{
			name:    "zero weight member gets nothing",
			weights: []memberWeight{{"a", 2}, {"b", 2}, {"z", 0}},
			buckets: 7,
			// exact 3.5,3.5,0 -> floors 3,3,0; remainder .5 tie a<b -> a
			want: map[string]int{"a": 4, "b": 3, "z": 0},
		},
		{
			name:    "single member takes all",
			weights: []memberWeight{{"solo", 5}},
			buckets: 137,
			want:    map[string]int{"solo": 137},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := allocateQuotas(tc.weights, tc.buckets)
			if err != nil {
				t.Fatalf("allocate: %v", err)
			}
			sum := 0
			for id, q := range got {
				sum += q
				if q != tc.want[id] {
					t.Fatalf("%s quota[%s]=%d want %d (full=%v)", tc.name, id, q, tc.want[id], got)
				}
			}
			if sum != tc.buckets {
				t.Fatalf("%s quota sum %d != buckets %d", tc.name, sum, tc.buckets)
			}
		})
	}
}

func TestAllocateQuotasRoundingBound(t *testing.T) {
	// For every quota q, |q - exact| < 1 and q >= floor(exact).
	weights := []memberWeight{{"a", 7}, {"b", 3}, {"c", 11}, {"d", 2}}
	total := 23
	for _, buckets := range []int{1, 2, 3, 7, 100, 999, 1000, 1024, 12345} {
		got, err := allocateQuotas(weights, buckets)
		if err != nil {
			t.Fatalf("buckets=%d: %v", buckets, err)
		}
		sum := 0
		for _, w := range weights {
			q := got[w.id]
			exact := float64(buckets) * float64(w.weight) / float64(total)
			if math.Abs(float64(q)-exact) >= 1.0 {
				t.Fatalf("buckets=%d member %s: quota %d too far from exact %.4f",
					buckets, w.id, q, exact)
			}
			if float64(q) < math.Floor(exact) {
				t.Fatalf("buckets=%d member %s: quota %d below floor %.4f",
					buckets, w.id, q, exact)
			}
			sum += q
		}
		if sum != buckets {
			t.Fatalf("buckets=%d sum %d", buckets, sum)
		}
	}
}

func TestAllocateQuotasErrors(t *testing.T) {
	if _, err := allocateQuotas(nil, 8); fherr.KindOf(err) != fherr.KindComputationFailed {
		t.Fatalf("empty: kind=%v", fherr.KindOf(err))
	}
	if _, err := allocateQuotas([]memberWeight{{"a", 0}, {"b", 0}}, 8); fherr.KindOf(err) != fherr.KindInput {
		t.Fatalf("all-zero must be input_error (explicit no-share state), got %v", err)
	}
	if _, err := allocateQuotas([]memberWeight{{"a", 1}}, 0); fherr.KindOf(err) != fherr.KindComputationFailed {
		t.Fatalf("zero buckets: kind=%v", fherr.KindOf(err))
	}
}

// Determinism: same inputs across repeated calls must give identical quotas
// regardless of Go map traversal (inputs are slices; this guards internals).
func TestAllocateQuotasDeterminism(t *testing.T) {
	first, _ := allocateQuotas(wm("a", "1", "b", "2", "c", "3", "d", "4"), 777)
	for i := 0; i < 50; i++ {
		again, _ := allocateQuotas(wm("d", "4", "c", "3", "b", "2", "a", "1"), 777)
		for id, q := range first {
			if again[id] != q {
				t.Fatalf("nondeterministic quota for %s: %d vs %d", id, q, again[id])
			}
		}
	}
}
