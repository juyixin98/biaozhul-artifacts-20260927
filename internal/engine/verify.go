package engine

import (
	"fmt"
	"math/big"

	"cidrcov/internal/netmodel"
)

// verify is the server-side guard that runs AFTER the cover is produced. It is
// deliberately written from interval primitives rather than reusing Cover:
//
//  1. every output prefix must be valid for the width, internally aligned and
//     mutually non-overlapping;
//  2. rebuilding intervals from the output and subtracting exclusions must
//     yield the EMPTY set in both directions
//     (cover \ (allow\exclude) == (allow\exclude) \ cover == 0), which is a
//     strict equality check, not a one-sided subset test;
//  3. no two output prefixes may be siblings whose aligned parent block is
//     fully contained in the cover — that pair would be mergeable and proves
//     the decomposition was not minimum.
//
// The exhaustive mathematical minimality proof in the test suite uses an
// independent bit-set reference; this runtime guard checks the same
// observable properties at real IPv6 width without enumerating addresses.
func verify(width int, allowed, excluded []netmodel.Interval, prefs []netmodel.Prefix) error {
	one := big.NewInt(1)

	// --- 1a. validity + alignment ----------------------------------------
	for i, p := range prefs {
		if p.Len < 0 || p.Len > width {
			return fmt.Errorf("prefix #%d has illegal length %d for width %d", i, p.Len, width)
		}
		h := uint(width - p.Len)
		size := new(big.Int).Lsh(one, h)
		if rem := new(big.Int).Mod(p.Base, size); rem.Sign() != 0 {
			return fmt.Errorf("prefix #%d %s/%d is not aligned", i, p.Base.String(), p.Len)
		}
		last := new(big.Int).Add(p.Base, new(big.Int).Sub(size, one))
		max := new(big.Int).Sub(new(big.Int).Lsh(one, uint(width)), one)
		if p.Base.Sign() < 0 || last.Cmp(max) > 0 {
			return fmt.Errorf("prefix #%d escapes the address space", i)
		}
	}

	// --- 1b. non-overlap (sorted by base) --------------------------------
	type span struct {
		start, end *big.Int
		len        int
	}
	spans := make([]span, len(prefs))
	for i, p := range prefs {
		size := new(big.Int).Lsh(one, uint(width-p.Len))
		spans[i] = span{
			start: new(big.Int).Set(p.Base),
			end:   new(big.Int).Sub(new(big.Int).Add(p.Base, size), one),
			len:   p.Len,
		}
	}
	// insertion sort is fine for the span order check inputs are already sorted;
	// copy a sorted view defensively.
	for i := 1; i < len(spans); i++ {
		for j := i; j > 0 && spans[j-1].start.Cmp(spans[j].start) > 0; j-- {
			spans[j-1], spans[j] = spans[j], spans[j-1]
		}
	}
	for i := 1; i < len(spans); i++ {
		if spans[i].start.Cmp(spans[i-1].end) <= 0 {
			return fmt.Errorf("overlap between prefix ending %s and prefix starting %s",
				spans[i-1].end.String(), spans[i].start.String())
		}
	}

	// --- 2. strict equality with allow\\exclude --------------------------
	target := netmodel.Subtract(allowed, excluded)
	rebuilt := netmodel.PrefixesToIntervals(width, prefs)
	extra := netmodel.Subtract(rebuilt, target)
	missing := netmodel.Subtract(target, rebuilt)
	if len(extra) != 0 {
		return fmt.Errorf("cover adds %d interval(s) not in allow\\exclude; first [%s..%s]",
			len(extra), extra[0].Start.String(), extra[0].End.String())
	}
	if len(missing) != 0 {
		return fmt.Errorf("cover misses %d interval(s) of allow\\exclude; first [%s..%s]",
			len(missing), missing[0].Start.String(), missing[0].End.String())
	}

	// --- 3. no mergeable siblings ----------------------------------------
	// Two prefixes of equal length L whose bases differ by exactly 2^(width-L)
	// and share an aligned parent (the lower base is parent-aligned) form a
	// sibling pair; their parent /(L-1) is contained in the cover iff no
	// address between their blocks is outside it. Rebuilding via union makes
	// adjacency imply containment, so check adjacency at equal lengths only.
	for i := 0; i+1 < len(spans); i++ {
		a, b := spans[i], spans[i+1]
		if a.len != b.len {
			continue
		}
		blockSize := new(big.Int).Add(new(big.Int).Sub(a.end, a.start), one)
		expectedNext := new(big.Int).Add(a.start, blockSize)
		if b.start.Cmp(expectedNext) != 0 {
			continue
		}
		// Parent alignment: a.start must be divisible by 2*blockSize.
		if rem := new(big.Int).Mod(a.start, new(big.Int).Lsh(blockSize, 1)); rem.Sign() != 0 {
			continue
		}
		return fmt.Errorf("mergeable siblings: %s/%d and %s/%d collapse to one /%d",
			a.start.String(), a.len, b.start.String(), b.len, a.len-1)
	}
	return nil
}
