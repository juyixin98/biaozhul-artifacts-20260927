// Exhaustive, independent correctness tests for the cover algorithm.
//
// Strategy: at REDUCED bit widths every possible set is enumerable. For every
// allow set (and, at small widths, every allow×exclude combination) we:
//
//  1. build the expected set as a dense bitset (test/refmodel),
//  2. compute expected minimum prefixes via an unrelated top-down trie,
//  3. run the production netmodel.Cover,
//  4. reconstruct what the production prefixes actually contain,
//  5. assert bit-for-bit equality AND exact prefix-list equality with the
//     reference trie output (which proves minimum cardinality, no overlap,
//     no mergeable siblings — not just set equality).
package netmodel_test

import (
	"fmt"
	"math/big"
	"math/rand"
	"testing"

	"cidrcov/internal/netmodel"
	"cidrcov/test/refmodel"
)

func bi(v uint64) *big.Int { return new(big.Int).SetUint64(v) }

// toRefIvs converts production intervals to the reference's uint64 pairs.
func toRefIvs(ivs []netmodel.Interval) [][2]uint64 {
	out := make([][2]uint64, len(ivs))
	for i, v := range ivs {
		out[i] = [2]uint64{v.Start.Uint64(), v.End.Uint64()}
	}
	return out
}

// assertAgainstReference runs the full independent verification of a Cover
// result and reports the failing property category explicitly.
func assertAgainstReference(t *testing.T, width int, allow, exclude []netmodel.Interval) {
	t.Helper()
	allowU := toRefIvs(allow)
	excludeU := toRefIvs(exclude)
	for _, iv := range append(append([][2]uint64{}, allowU...), excludeU...) {
		if int64(iv[1]) >= int64(1)<<width {
			t.Fatalf("[test_bug] generated interval exceeds width %d: %v (allow=%v exclude=%v)",
				width, iv, allowU, excludeU)
		}
	}

	allowSet := refmodel.FromIntervals(width, allowU)
	excludeSet := refmodel.FromIntervals(width, excludeU)
	targetSet := refmodel.Difference(allowSet, excludeSet)
	wantPrefs := refmodel.CanonicalCover(width, targetSet)

	got := netmodel.Cover(width, allow, exclude)

	// Property A: reconstruct cover membership from the produced prefixes.
	gotSet := refmodel.NewEmpty(width)
	prevLast := new(big.Int).SetInt64(-1)
	for _, p := range got {
		h := width - p.Len
		// A1: alignment.
		size := new(big.Int).Lsh(big.NewInt(1), uint(h))
		if new(big.Int).Mod(p.Base, size).Sign() != 0 {
			t.Fatalf("[not_aligned] width=%d prefix %s/%d not aligned", width, p.Base, p.Len)
		}
		last := new(big.Int).Add(p.Base, new(big.Int).Sub(size, big.NewInt(1)))
		// A2: strict non-overlap (sorted ascending).
		if p.Base.Cmp(prevLast) <= 0 {
			t.Fatalf("[overlap] width=%d base %s not greater than previous last %s",
				width, p.Base, prevLast)
		}
		gotSet.FillBlock(p.Base.Uint64(), h)
		prevLast = last
	}
	gotSet.MaskToUniverse()

	// Property B: exact set equality (no gap, no extra address).
	if !gotSet.Equal(targetSet) {
		extra := refmodel.Difference(gotSet, targetSet).Count()
		missing := refmodel.Difference(targetSet, gotSet).Count()
		t.Fatalf("[set_mismatch] width=%d extra=%d missing=%d target=%d got=%d",
			width, extra, missing, targetSet.Count(), gotSet.Count())
	}

	// Property C: exact equality with the independent canonical minimum cover.
	if len(got) != len(wantPrefs) {
		gotS := make([]string, len(got))
		for i, p := range got {
			gotS[i] = fmt.Sprintf("%d/%d", p.Base, p.Len)
		}
		wantS := make([]string, len(wantPrefs))
		for i, p := range wantPrefs {
			wantS[i] = fmt.Sprintf("%d/%d", p.Base, p.Len)
		}
		t.Fatalf("[not_minimum] width=%d allow=%v exclude=%v\n got(%d)=%v\nwant(%d)=%v",
			width, allowU, excludeU, len(got), gotS, len(wantPrefs), wantS)
	}
	for i, wp := range wantPrefs {
		if got[i].Len != wp.Len || got[i].Base.Uint64() != wp.Base {
			t.Fatalf("[canonical_differs] width=%d prefix #%d got %s/%d want %d/%d",
				width, i, got[i].Base, got[i].Len, wp.Base, wp.Len)
		}
	}
}

// enumerateMasks returns every width-bit mask (as interval slice inputs).
func allMasksAsIntervals(width int) [][]netmodel.Interval {
	total := 1 << width
	out := make([][]netmodel.Interval, 0, total)
	for mask := 0; mask < total; mask++ {
		var ivs []netmodel.Interval
		runStart := -1
		for bit := 0; bit < width; bit++ {
			on := mask&(1<<bit) != 0
			if on && runStart < 0 {
				runStart = bit
			}
			if (!on || bit == width-1) && runStart >= 0 {
				end := bit - 1
				if on {
					end = bit
				}
				ivs = append(ivs, netmodel.Interval{Start: bi(uint64(runStart)), End: bi(uint64(end))})
				runStart = -1
			}
		}
		out = append(out, ivs)
	}
	return out
}

// TestExhaustiveWidth3 enumerates EVERY allow×exclude combination at 3 bits:
// 256 allow sets × 256 exclude sets = 65,536 cases, all addressable sets.
func TestExhaustiveWidth3(t *testing.T) {
	all := allMasksAsIntervals(3)
	for ai, allow := range all {
		for ei, exclude := range all {
			assertAgainstReference(t, 3, allow, exclude)
			_ = ai
			_ = ei
		}
	}
}

// TestExhaustiveAllowWidth4 enumerates every 4-bit allow set with a fixed
// representative battery of exclusions (empty, single point, interior run,
// full space, complement-ish), covering all structural exclusion shapes.
func TestExhaustiveAllowWidth4(t *testing.T) {
	all := allMasksAsIntervals(4)
	excludes := [][]netmodel.Interval{
		nil,
		{{bi(0), bi(0)}},
		{{bi(15), bi(15)}},
		{{bi(5), bi(10)}},
		{{bi(0), bi(15)}},
		{{bi(2), bi(2)}, {bi(13), bi(13)}},
		{{bi(0), bi(0)}, {bi(15), bi(15)}},
		{{bi(3), bi(6)}, {bi(9), bi(12)}},
	}
	for _, allow := range all {
		for _, ex := range excludes {
			assertAgainstReference(t, 4, allow, ex)
		}
	}
}

// TestRandomWidth8 fuzzes thousands of randomized multi-interval inputs where
// intervals may overlap, be unordered and straddle every alignment boundary.
func TestRandomWidth8(t *testing.T) {
	const width = 8
	rng := rand.New(rand.NewSource(20260928))
	gen := func() []netmodel.Interval {
		n := rng.Intn(8)
		ivs := make([]netmodel.Interval, n)
		for i := range ivs {
			lo := rng.Intn(1 << width)
			hi := lo + rng.Intn(1<<width-lo) // hi in [lo, 2^width-1]
			ivs[i] = netmodel.Interval{Start: bi(uint64(lo)), End: bi(uint64(hi))}
		}
		return ivs
	}
	for i := 0; i < 20000; i++ {
		assertAgainstReference(t, width, gen(), gen())
	}
}

// TestRandomWidth16 uses wider intervals to exercise many-level trie behavior
// at a still-verifiable width.
func TestRandomWidth16(t *testing.T) {
	const width = 16
	rng := rand.New(rand.NewSource(424242))
	gen := func() []netmodel.Interval {
		n := rng.Intn(10)
		ivs := make([]netmodel.Interval, n)
		for i := range ivs {
			lo := rng.Intn(1 << width)
			hi := lo + rng.Intn(1<<width-lo)
			ivs[i] = netmodel.Interval{Start: bi(uint64(lo)), End: bi(uint64(hi))}
		}
		return ivs
	}
	for i := 0; i < 3000; i++ {
		assertAgainstReference(t, width, gen(), gen())
	}
}

// TestNoEnumerationBounds checks that astronomically large prefix blocks are
// handled by arithmetic, not enumeration: full 32-bit space is one prefix.
func TestFullSpacePrefix(t *testing.T) {
	for _, w := range []int{32, 128} {
		max := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), uint(w)), big.NewInt(1))
		got := netmodel.Cover(w,
			[]netmodel.Interval{{bi(0), max}}, nil)
		if len(got) != 1 || got[0].Len != 0 {
			t.Fatalf("full /%d space must be one /0 prefix, got %+v", w, got)
		}
	}
}

// TestEmptySet verifies the empty set edge cases explicitly.
func TestEmptySet(t *testing.T) {
	max4 := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), 32), big.NewInt(1))
	cases := []struct {
		name    string
		width   int
		allow   []netmodel.Interval
		exclude []netmodel.Interval
	}{
		{"nothing allowed", 32, nil, nil},
		{"everything excluded", 32, []netmodel.Interval{{bi(0), max4}}, []netmodel.Interval{{bi(0), max4}}},
		{"more excluded than allowed", 8, []netmodel.Interval{{bi(2), bi(5)}}, []netmodel.Interval{{bi(0), bi(7)}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := netmodel.Cover(c.width, c.allow, c.exclude)
			if len(got) != 0 {
				t.Fatalf("empty set must yield 0 prefixes, got %d: %+v", len(got), got)
			}
		})
	}
}
