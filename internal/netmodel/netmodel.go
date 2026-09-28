// Package netmodel implements width-independent large-integer interval
// arithmetic over address spaces and the decomposition of arbitrary interval
// unions into a minimal, non-overlapping CIDR (prefix) cover.
//
// The same code paths serve both IPv4 (width 32) and IPv6 (width 128); no
// address is ever enumerated. All integers are math/big.Int values in the
// range [0, 2^width-1].
package netmodel

import (
	"math/big"
	"sort"
)

// Interval is a closed integer address range [Start, End].
type Interval struct {
	Start *big.Int
	End   *big.Int
}

// Prefix is a fixed-width CIDR prefix: Base is the network address with host
// bits already zero and Len is the prefix length in bits (0..width).
//
// A prefix ALWAYS denotes the whole aligned block, i.e. it includes network
// and broadcast addresses. There is no /31 or /32 special casing anywhere:
// 10.0.0.0/30 is exactly four addresses {.0,.1,.2,.3}. This policy is fixed
// and is asserted by the independent test reference.
type Prefix struct {
	Base *big.Int
	Len  int
}

func dup(i *big.Int) *big.Int { return new(big.Int).Set(i) }

// Union merges a slice of intervals into a sorted slice of maximal,
// disjoint, non-adjacent-normalized intervals. Adjacent intervals (e.g.
// [0,1] and [2,3]) are merged because they represent one contiguous run of
// addresses, which is what prefix decomposition consumes.
//
// Input intervals may be supplied in any order and may overlap. The caller
// retains ownership of the big.Int values it passes in.
func Union(in []Interval) []Interval {
	if len(in) == 0 {
		return nil
	}
	ivs := make([]Interval, len(in))
	for i, v := range in {
		ivs[i] = Interval{Start: dup(v.Start), End: dup(v.End)}
	}
	sort.Slice(ivs, func(i, j int) bool { return ivs[i].Start.Cmp(ivs[j].Start) < 0 })
	out := make([]Interval, 0, len(ivs))
	cur := ivs[0]
	for _, nxt := range ivs[1:] {
		// Overlap or adjacency: nxt.Start <= cur.End+1.
		if nxt.Start.Cmp(new(big.Int).Add(cur.End, big.NewInt(1))) <= 0 {
			if nxt.End.Cmp(cur.End) > 0 {
				cur.End = nxt.End
			}
			continue
		}
		out = append(out, cur)
		cur = nxt
	}
	out = append(out, cur)
	return out
}

// Subtract returns a \\ b: the parts of the sorted, disjoint interval set a
// not covered by b. Both inputs must already be the product of Union (sorted,
// disjoint, adjacency-merged); the function is a single linear sweep and never
// enumerates addresses.
func Subtract(a, b []Interval) []Interval {
	var out []Interval
	j := 0
	for i := 0; i < len(a); i++ {
		cs, ce := dup(a[i].Start), dup(a[i].End)
		// b is consumed monotonically: any exclusion ending strictly
		// before this allowed interval can never matter again.
		for j < len(b) && b[j].End.Cmp(cs) < 0 {
			j++
		}
		k := j
		cur := cs
		for k < len(b) && b[k].Start.Cmp(ce) <= 0 {
			bs, be := b[k].Start, b[k].End
			if bs.Cmp(cur) > 0 {
				out = append(out, Interval{Start: dup(cur), End: dup(new(big.Int).Sub(bs, big.NewInt(1)))})
			}
			if be.Cmp(cur) >= 0 {
				cur = new(big.Int).Add(be, big.NewInt(1))
			}
			k++
			if cur.Cmp(ce) > 0 {
				break
			}
		}
		if cur.Cmp(ce) <= 0 {
			out = append(out, Interval{Start: dup(cur), End: dup(ce)})
		}
	}
	return out
}

// PrefixToInterval returns the closed interval a prefix covers:
// [base & ~(2^h-1), base + 2^h - 1] where h = width-len. The returned base is
// the realigned network address, so callers feeding non-canonical input can
// detect that realignment happened.
func PrefixToInterval(width, prefixLen int, baseArg *big.Int) (network *big.Int, last *big.Int, ok bool) {
	if prefixLen < 0 || prefixLen > width {
		return nil, nil, false
	}
	if baseArg.Sign() < 0 {
		return nil, nil, false
	}
	h := uint(width - prefixLen)
	size := new(big.Int).Lsh(big.NewInt(1), h) // 2^h
	mask := new(big.Int).Sub(size, big.NewInt(1))
	max := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), uint(width)), big.NewInt(1))
	if baseArg.Cmp(max) > 0 {
		return nil, nil, false
	}
	net := new(big.Int).AndNot(baseArg, mask)
	lastAddr := new(big.Int).Add(net, mask)
	return net, lastAddr, true
}

// IntervalToPrefixes greedily decomposes one closed interval [lo, hi] into
// prefixes. At each step it chooses the LARGEST aligned block fitting at the
// current cursor: prefix length = max(length_of_run, alignment_of_cursor).
//
// Greedy largest-first yields the unique canonical minimum-cardinality prefix
// cover of an interval. Minimality for unions of disjoint intervals follows
// because the blocks of distinct result intervals never share a parent block
// (that parent would be contained in their gap-free union only if the
// intervals were adjacent, but Union already merged adjacencies).
func IntervalToPrefixes(width int, lo, hi *big.Int) []Prefix {
	if lo.Cmp(hi) > 0 {
		return nil
	}
	var out []Prefix
	cur := dup(lo)
	one := big.NewInt(1)
	for cur.Cmp(hi) <= 0 {
		remain := new(big.Int).Sub(hi, cur) // remaining span minus one
		// Largest h with 2^h <= remain+1 (run-length constraint):
		// h = floor(log2(remain+1)) = BitLen(remain+1)-1.
		runH := new(big.Int).Add(remain, one).BitLen() - 1
		// Largest h with cur divisible by 2^h (alignment constraint): the
		// trailing-zero count of cur, or the full width when cur == 0.
		alignH := width
		if cur.Sign() != 0 {
			alignH = trailingZeroBits(cur)
		}
		h := runH
		if alignH < h {
			h = alignH
		}
		out = append(out, Prefix{Base: dup(cur), Len: width - h})
		cur.Add(cur, new(big.Int).Lsh(one, uint(h)))
	}
	return out
}

func trailingZeroBits(x *big.Int) int {
	n := 0
	for _, w := range x.Bits() {
		if w == 0 {
			n += 64
			continue
		}
		n += trailingZeros64(uint64(w))
		return n
	}
	// x == 0: effectively infinite; callers handle zero before reaching here.
	return n
}

func trailingZeros64(x uint64) int {
	if x == 0 {
		return 64
	}
	n := 0
	if x&0xffffffff == 0 {
		n += 32
		x >>= 32
	}
	if x&0xffff == 0 {
		n += 16
		x >>= 16
	}
	if x&0xff == 0 {
		n += 8
		x >>= 8
	}
	if x&0xf == 0 {
		n += 4
		x >>= 4
	}
	if x&0x3 == 0 {
		n += 2
		x >>= 2
	}
	if x&0x1 == 0 {
		n++
	}
	return n
}

// Cover computes the minimal non-overlapping prefix cover of (union(allows)
// minus union(excludes)) for a fixed address width.
//
// allowIntervals and excludeIntervals are raw closed intervals; they are
// normalized internally. The result prefixes are sorted ascending, disjoint
// and merge-free (no two are siblings whose parent lies inside the cover).
func Cover(width int, allowIntervals, excludeIntervals []Interval) []Prefix {
	allowed := Union(allowIntervals)
	if len(allowed) == 0 {
		return nil
	}
	excluded := Union(excludeIntervals)
	diff := Subtract(allowed, excluded)
	var out []Prefix
	for _, iv := range diff {
		out = append(out, IntervalToPrefixes(width, iv.Start, iv.End)...)
	}
	return out
}

// PrefixesToIntervals is the inverse used by verification paths: it expands a
// sorted-or-unsorted prefix list back into normalized closed intervals.
func PrefixesToIntervals(width int, ps []Prefix) []Interval {
	ivs := make([]Interval, 0, len(ps))
	for _, p := range ps {
		net, last, ok := PrefixToInterval(width, p.Len, p.Base)
		if !ok {
			continue
		}
		ivs = append(ivs, Interval{Start: net, End: last})
	}
	return Union(ivs)
}
