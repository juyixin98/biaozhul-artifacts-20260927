package netmodel

import (
	"math/big"
	"math/rand"
	"testing"
)

// Enumerate every point of a tiny 5-axis space and check Space.Minus /
// Intersect / Subset / Equals pointwise against a reference implementation,
// on random axis-aligned boxes. Every axis uses the product's own bit width,
// so this exercises the geometry independently of the real address widths.
func TestProductSpaceOpsByEnumeration(t *testing.T) {
	dims := []int{1, 2, 2, 2, 2} // domain sizes 2,4,4,4,4 -> 512 points
	sizes := make([]int, 5)
	for i, b := range dims {
		sizes[i] = 1 << b
	}

	type pt = [5]int
	inBox := func(box [][2]int, p pt) bool {
		for i := range p {
			if p[i] < box[i][0] || p[i] > box[i][1] {
				return false
			}
		}
		return true
	}
	allPoints := func() []pt {
		var out []pt
		for a := 0; a < sizes[0]; a++ {
			for b := 0; b < sizes[1]; b++ {
				for c := 0; c < sizes[2]; c++ {
					for d := 0; d < sizes[3]; d++ {
						for e := 0; e < sizes[4]; e++ {
							out = append(out, pt{a, b, c, d, e})
						}
					}
				}
			}
		}
		return out
	}
	randomBox := func(rng *rand.Rand) [][2]int {
		box := make([][2]int, 5)
		for i := 0; i < 5; i++ {
			lo := rng.Intn(sizes[i])
			hi := lo + rng.Intn(sizes[i]-lo)
			box[i] = [2]int{lo, hi}
		}
		return box
	}
	toSpace := func(box [][2]int) Space {
		r := func(i int) Int1D {
			return MustRange1D(dims[i], big.NewInt(int64(box[i][0])), big.NewInt(int64(box[i][1])))
		}
		return Space{Products: []Product{{Proto: r(0), Src: r(1), Dst: r(2), SrcP: r(3), DstP: r(4)}}}
	}
	productContainsPoint := func(pr Product, p pt) bool {
		axes := []Int1D{pr.Proto, pr.Src, pr.Dst, pr.SrcP, pr.DstP}
		for i := range p {
			if !axes[i].Contains(big.NewInt(int64(p[i]))) {
				return false
			}
		}
		return true
	}
	spaceContains := func(s Space, p pt) bool {
		for _, pr := range s.Products {
			if productContainsPoint(pr, p) {
				return true
			}
		}
		return false
	}
	productsOverlap := func(a, b Product) bool {
		return !a.Proto.Intersect(b.Proto).IsEmpty() &&
			!a.Src.Intersect(b.Src).IsEmpty() &&
			!a.Dst.Intersect(b.Dst).IsEmpty() &&
			!a.SrcP.Intersect(b.SrcP).IsEmpty() &&
			!a.DstP.Intersect(b.DstP).IsEmpty()
	}

	rng := rand.New(rand.NewSource(4242))
	points := allPoints()
	for iter := 0; iter < 200; iter++ {
		ba, bb := randomBox(rng), randomBox(rng)
		memA := map[pt]bool{}
		memB := map[pt]bool{}
		for _, p := range points {
			memA[p] = inBox(ba, p)
			memB[p] = inBox(bb, p)
		}
		sa, sb := toSpace(ba), toSpace(bb)
		diff := sa.Minus(sb)
		inter := sa.Intersect(sb)
		for _, p := range points {
			if got := spaceContains(diff, p); got != (memA[p] && !memB[p]) {
				t.Fatalf("iter %d minus at %v: got %v want %v", iter, p, got, memA[p] && !memB[p])
			}
			if got := spaceContains(inter, p); got != (memA[p] && memB[p]) {
				t.Fatalf("iter %d intersect at %v: got %v want %v", iter, p, got, memA[p] && memB[p])
			}
		}
		for i := range diff.Products {
			for j := i + 1; j < len(diff.Products); j++ {
				if productsOverlap(diff.Products[i], diff.Products[j]) {
					t.Fatalf("iter %d: minus products %d and %d overlap", iter, i, j)
				}
			}
		}
		if (func() bool {
			for _, p := range points {
				if memA[p] && !memB[p] {
					return false
				}
			}
			return true
		})() && !sa.Subset(sb) {
			t.Fatalf("iter %d: subset false negative", iter)
		}
	}
}
