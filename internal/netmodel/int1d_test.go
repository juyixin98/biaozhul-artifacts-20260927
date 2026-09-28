package netmodel

import (
	"math/big"
	"math/rand"
	"testing"
)

func TestIntervalSetOpsByEnumeration(t *testing.T) {
	const bits = 4
	const N = 1 << bits // 16
	mk := func(lo, hi int64) Int1D { return MustRange1D(bits, big.NewInt(lo), big.NewInt(hi)) }

	// Hand cases: crossing intervals.
	a := mk(2, 9)
	b := mk(7, 12)
	diff := a.Minus(b)
	for v := 0; v < N; v++ {
		want := v >= 2 && v <= 9 && !(v >= 7 && v <= 12)
		if got := diff.Contains(big.NewInt(int64(v))); got != want {
			t.Fatalf("minus membership at %d: got %v want %v", v, got, want)
		}
	}
	if got := a.Minus(a); !got.IsEmpty() {
		t.Fatalf("a\\a must be empty")
	}
	un := Universe1D(bits)
	if !un.Minus(mk(0, 7)).Equals(mk(8, 15)) {
		t.Fatalf("universe minus half != other half")
	}

	// Randomized: compare set algebra against a bitmask oracle.
	rng := rand.New(rand.NewSource(20260928))
	for iter := 0; iter < 200; iter++ {
		x := randomSet(rng, bits)
		y := randomSet(rng, bits)
		var mx, my uint32
		for v := 0; v < N; v++ {
			if x.Contains(big.NewInt(int64(v))) {
				mx |= 1 << v
			}
			if y.Contains(big.NewInt(int64(v))) {
				my |= 1 << v
			}
		}
		check := func(name string, s Int1D, mask uint32) {
			for v := 0; v < N; v++ {
				want := mask&(1<<v) != 0
				if got := s.Contains(big.NewInt(int64(v))); got != want {
					t.Fatalf("iter %d %s membership %d: got %v want %v", iter, name, v, got, want)
				}
			}
		}
		check("minus", x.Minus(y), mx & ^my)
		check("intersect", x.Intersect(y), mx&my)
		check("union", FromSegmentsUnion(x, y), mx|my)
		if (mx & ^my) == 0 && !x.Subset(y) {
			t.Fatalf("subset false negative")
		}
	}
}

func randomSet(rng *rand.Rand, bits int) Int1D {
	n := rng.Intn(4)
	var segs []Seg
	for i := 0; i < n; i++ {
		lo := rng.Intn(1 << bits)
		hi := lo + rng.Intn(1<<bits-lo)
		segs = append(segs, Seg{big.NewInt(int64(lo)), big.NewInt(int64(hi))})
	}
	s, err := FromSegments(bits, segs)
	if err != nil {
		panic(err)
	}
	return s
}

// FromSegmentsUnion is test sugar: union via normalized segments.
func FromSegmentsUnion(a, b Int1D) Int1D {
	s, err := FromSegments(a.Bits(), append(append([]Seg{}, a.Segments()...), b.Segments()...))
	if err != nil {
		panic(err)
	}
	return s
}

func TestAdjacentMergeAndNormalization(t *testing.T) {
	s, err := FromSegments(8, []Seg{
		{big.NewInt(10), big.NewInt(20)},
		{big.NewInt(21), big.NewInt(30)}, // adjacent -> merged
		{big.NewInt(5), big.NewInt(9)},   // adjacent below -> merged
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Segments()) != 1 {
		t.Fatalf("expected single merged segment, got %d", len(s.Segments()))
	}
}
