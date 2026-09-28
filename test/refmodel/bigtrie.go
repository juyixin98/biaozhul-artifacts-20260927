package refmodel

import (
	"math/big"
	"sort"
)

// BigPrefix is a prefix in big-integer coordinates.
type BigPrefix struct {
	Base *big.Int
	Len  int
}

// BigInterval is a closed [Lo,Hi] interval of big integers.
type BigInterval struct {
	Lo, Hi *big.Int
}

func bdup(x *big.Int) *big.Int { return new(big.Int).Set(x) }

// BigNormalize is the reference's OWN interval union (sweep line), written
// independently of production code.
func BigNormalize(in []BigInterval) []BigInterval {
	cp := make([]BigInterval, len(in))
	for i, v := range in {
		cp[i] = BigInterval{Lo: bdup(v.Lo), Hi: bdup(v.Hi)}
	}
	sort.Slice(cp, func(i, j int) bool { return cp[i].Lo.Cmp(cp[j].Lo) < 0 })
	var out []BigInterval
	for _, iv := range cp {
		if n := len(out); n > 0 {
			// merge if iv.Lo <= out.last.Hi+1 (adjacency merges)
			if iv.Lo.Cmp(new(big.Int).Add(out[n-1].Hi, big.NewInt(1))) <= 0 {
				if iv.Hi.Cmp(out[n-1].Hi) > 0 {
					out[n-1].Hi = iv.Hi
				}
				continue
			}
		}
		out = append(out, iv)
	}
	return out
}

// BigDifference is the reference's OWN interval subtraction.
func BigDifference(a, b []BigInterval) []BigInterval {
	var out []BigInterval
	for _, x := range a {
		parts := []BigInterval{{Lo: bdup(x.Lo), Hi: bdup(x.Hi)}}
		for _, y := range b {
			var next []BigInterval
			for _, p := range parts {
				if y.Hi.Cmp(p.Lo) < 0 || y.Lo.Cmp(p.Hi) > 0 {
					next = append(next, p) // disjoint
					continue
				}
				if y.Lo.Cmp(p.Lo) <= 0 && y.Hi.Cmp(p.Hi) >= 0 {
					continue // fully removed
				}
				if y.Lo.Cmp(p.Lo) > 0 {
					next = append(next, BigInterval{p.Lo, new(big.Int).Sub(y.Lo, big.NewInt(1))})
				}
				if y.Hi.Cmp(p.Hi) < 0 {
					next = append(next, BigInterval{new(big.Int).Add(y.Hi, big.NewInt(1)), p.Hi})
				}
			}
			parts = next
		}
		out = append(out, parts...)
	}
	return BigNormalize(out)
}

// BigTrieCover is an independent minimum-cover oracle valid at ANY width up to
// thousands of bits (it recurses structurally, never enumerating addresses).
// It uses top-down trie classification: a block is FULL (emit one prefix),
// EMPTY (skip) or PARTIAL (descend both halves). The emitted cover is the
// unique canonical minimum-cardinality prefix cover.
func BigTrieCover(width int, allow, exclude []BigInterval) []BigPrefix {
	target := BigDifference(BigNormalize(allow), BigNormalize(exclude))
	var out []BigPrefix
	one := big.NewInt(1)

	var rec func(start *big.Int, h int)
	rec = func(start *big.Int, h int) {
		size := new(big.Int).Lsh(one, uint(h))
		last := new(big.Int).Sub(new(big.Int).Add(start, size), one)
		state := classify(target, start, last)
		switch state {
		case +1:
			out = append(out, BigPrefix{Base: bdup(start), Len: width - h})
		case 0:
			if h == 0 {
				// A single address that is neither full nor empty cannot happen.
				panic("refmodel: undecidable leaf")
			}
			half := new(big.Int).Lsh(one, uint(h-1))
			rec(start, h-1)
			rec(new(big.Int).Add(start, half), h-1)
		}
	}
	rec(big.NewInt(0), width)
	return out
}

// classify returns +1 if [lo,hi] is fully contained in target, -1 if fully
// disjoint, 0 if partially covered.
func classify(target []BigInterval, lo, hi *big.Int) int {
	intersects := false
	for _, iv := range target {
		if iv.Hi.Cmp(lo) >= 0 && iv.Lo.Cmp(hi) <= 0 {
			intersects = true
			// Containment: this (normalized) interval spans the whole block.
			if iv.Lo.Cmp(lo) <= 0 && iv.Hi.Cmp(hi) >= 0 {
				return +1
			}
		}
	}
	if !intersects {
		return -1
	}
	return 0
}
