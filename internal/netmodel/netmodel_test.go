package netmodel

import (
	"math/big"
	"math/rand"
	"testing"
)

// ---------------------------------------------------------------------------
// Independent reference oracles
//
// The tests never derive expected answers from the production Cover/Subtract
// code. Two independent constructions are used and cross-checked:
//
//	bitsetTruth: ground truth at widths <= 4 by enumerating EVERY address and
//	             computing prefix membership with grade-school floor division.
//	trieOracle:  post-order labeling of the full binary prefix trie using only
//	             interval containment against target ranges (no enumeration);
//	             works at width 32/128. At small widths it is itself validated
//	             bit-for-bit against bitsetTruth.
// ---------------------------------------------------------------------------

// prefixContains is the per-address membership oracle: addr is in the block
// iff floor(addr/2^(w-p))*2^(w-p) == network. Nothing in production is used.
func prefixContains(network int64, prefixLen, width, addr int) bool {
	size := 1 << (width - prefixLen)
	return (addr/size)*size == int(network)
}

// targetFromBitset converts a bitset (bit i == address i) into runs.
func targetFromBitset(set *big.Int, width int) []Range {
	var rs []Range
	n := new(big.Int).Lsh(big.NewInt(1), uint(width))
	for i := big.NewInt(0); i.Cmp(n) < 0; i.Add(i, big.NewInt(1)) {
		if set.Bit(int(i.Int64())) == 0 {
			continue
		}
		start := new(big.Int).Set(i)
		end := new(big.Int).Add(start, big.NewInt(1))
		for end.Cmp(n) < 0 && set.Bit(int(end.Int64())) == 1 {
			end.Add(end, big.NewInt(1))
		}
		rs = append(rs, Range{Start: start, End: new(big.Int).Set(end)})
		i.Set(new(big.Int).Sub(end, big.NewInt(1)))
	}
	return rs
}

// trieOracle returns the minimal cover by labeling the full binary prefix
// trie: a node is emitted iff its whole interval is contained in one target
// range and its parent's is not; nodes disjoint from the target are pruned.
// This is a different algorithm from the production left-to-right greedy walk.
func trieOracle(target []Range, width int) []Block {
	target = localNormalize(cloneRanges(target))
	var out []Block
	var rec func(lo *big.Int, depth int)
	rec = func(lo *big.Int, depth int) {
		hi := new(big.Int).Add(lo, new(big.Int).Lsh(big.NewInt(1), uint(width-depth)))
		switch classifyNode(lo, hi, target) {
		case nodeFull:
			out = append(out, Block{Network: new(big.Int).Set(lo), PrefixLen: depth})
			return
		case nodeEmpty:
			return
		}
		if depth == width {
			return // a leaf cannot be mixed
		}
		half := new(big.Int).Lsh(big.NewInt(1), uint(width-depth-1))
		rec(new(big.Int).Set(lo), depth+1)
		rec(new(big.Int).Add(lo, half), depth+1)
	}
	rec(big.NewInt(0), 0)
	return out
}

type nodeKind int

const (
	nodeMixed nodeKind = iota
	nodeEmpty
	nodeFull
)

func classifyNode(lo, hi *big.Int, target []Range) nodeKind {
	intersects := false
	for _, r := range target {
		if r.End.Cmp(lo) <= 0 || r.Start.Cmp(hi) >= 0 {
			continue
		}
		intersects = true
		// Fully contained in this range?
		if r.Start.Cmp(lo) <= 0 && r.End.Cmp(hi) >= 0 {
			return nodeFull
		}
	}
	if !intersects {
		return nodeEmpty
	}
	return nodeMixed
}

// blocksEqual compares two block decompositions as sets (both outputs are
// sorted by construction, but do not rely on that).
func blocksEqual(a, b []Block) bool {
	key := func(bl Block) [2]string { return [2]string{bl.Network.String(), itoa(bl.PrefixLen)} }
	ma := map[[2]string]int{}
	mb := map[[2]string]int{}
	for _, bl := range a {
		ma[key(bl)]++
	}
	for _, bl := range b {
		mb[key(bl)]++
	}
	if len(ma) != len(mb) {
		return false
	}
	for k, v := range ma {
		if mb[k] != v {
			return false
		}
	}
	return true
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}

func blocksOf(rs []Range, width int) []Block { return Cover(rs, width) }

// checkOne runs the full assertion battery for one target bitset.
func checkOne(t *testing.T, set *big.Int, width int) {
	t.Helper()
	target := targetFromBitset(set, width)

	got := blocksOf(target, width)
	want := trieOracle(target, width)
	if !blocksEqual(got, want) {
		t.Fatalf("width=%d target=%s\n got=%v\nwant=%v", width, set.Text(2), got, want)
	}

	violations, proof := VerifyCover(got, target, width)
	if len(violations) != 0 {
		t.Fatalf("width=%d target=%s verification violations: %v", width, set.Text(2), violations)
	}
	// Address-count cross check against the bitset popcount.
	if proof.CoverAddressCount != itoaBig(popcount(set)) {
		t.Fatalf("width=%d count: proof=%s bitset=%d", width, proof.CoverAddressCount, popcount(set))
	}
	if !proof.Equivalent {
		t.Fatalf("width=%d equivalence flag false", width)
	}
	if len(proof.SiblingMerges) != 0 {
		t.Fatalf("width=%d mergeable siblings: %v", width, proof.SiblingMerges)
	}
}

func popcount(set *big.Int) int {
	n := 0
	for i := 0; i < set.BitLen(); i++ {
		n += int(set.Bit(i))
	}
	return n
}

func itoaBig(n int) string { return big.NewInt(int64(n)).String() }

// ---------------------------------------------------------------------------
// Exhaustive tests at reduced bit widths.
// ---------------------------------------------------------------------------

// TestExhaustiveWidth3 enumerates EVERY possible target subset of the 8-address
// space (256 cases).
func TestExhaustiveWidth3(t *testing.T) {
	const w = 3
	for mask := 0; mask < 1<<(1<<w); mask++ {
		checkOne(t, big.NewInt(int64(mask)), w)
	}
}

// TestExhaustiveWidth4 enumerates EVERY target subset of the 16-address space
// (65536 cases): full space, empty set, every interleaving. Skipped under
// -short; the width-3 sweep (256 cases), pair sweep and fuzz still run.
func TestExhaustiveWidth4(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping 65536-case exhaustive sweep in -short mode")
	}
	const w = 4
	for mask := int64(0); mask < 1<<(1<<w); mask++ {
		checkOne(t, big.NewInt(mask), w)
	}
}

// TestTrieOracleAgreesWithEnumeration is the oracle self-check at widths 3 and
// 4: the trie decomposition rebuilt into a bitset must equal the truth bitset.
func TestTrieOracleAgreesWithEnumeration(t *testing.T) {
	for _, w := range []int{3, 4} {
		for mask := 0; mask < 1<<(1<<w); mask++ {
			set := big.NewInt(int64(mask))
			target := targetFromBitset(set, w)
			var rebuilt big.Int
			for _, b := range trieOracle(target, w) {
				r := BlockRange(b, w)
				for x := new(big.Int).Set(r.Start); x.Cmp(r.End) < 0; x.Add(x, big.NewInt(1)) {
					rebuilt.SetBit(&rebuilt, int(x.Int64()), 1)
				}
			}
			if rebuilt.Cmp(set) != 0 {
				t.Fatalf("w=%d mask=%0*b oracle=%b", w, 1<<w, mask, &rebuilt)
			}
		}
	}
}

// ---------------------------------------------------------------------------
// Subtract semantics: exhaustive / fuzzed allow-minus-exclude inputs.
// ---------------------------------------------------------------------------

// allPrefixes enumerates every legal CIDR block of a small width.
func allPrefixes(width int) []struct {
	net int64
	p   int
} {
	var out []struct {
		net int64
		p   int
	}
	for p := 0; p <= width; p++ {
		for n := 0; n < (1 << p); n++ {
			out = append(out, struct {
				net int64
				p   int
			}{int64(n << (width - p)), p})
		}
	}
	return out
}

// evaluateSet computes membership of every address from allow/exclude CIDR
// lists using ONLY per-address floor-division membership — no production set
// code — and returns the truth bitset plus production ranges.
func evaluateSet(t *testing.T, allow, exclude []struct {
	net int64
	p   int
}, width int) (*big.Int, []Range) {
	t.Helper()
	truth := new(big.Int)
	for addr := 0; addr < 1<<width; addr++ {
		inAllow := false
		for _, c := range allow {
			if prefixContains(c.net, c.p, width, addr) {
				inAllow = true
				break
			}
		}
		inExclude := false
		for _, c := range exclude {
			if prefixContains(c.net, c.p, width, addr) {
				inExclude = true
				break
			}
		}
		if inAllow && !inExclude {
			truth.SetBit(truth, addr, 1)
		}
	}
	mk := func(list []struct {
		net int64
		p   int
	}) []Range {
		rs := make([]Range, len(list))
		for i, c := range list {
			start := big.NewInt(c.net)
			end := new(big.Int).Add(start, big.NewInt(int64(1<<(width-c.p))))
			rs[i] = Range{Start: start, End: end}
		}
		return rs
	}
	got := Subtract(mk(allow), mk(exclude), width)
	return truth, got
}

// TestSubtractWidth3AllowSubsets: every allow subset (32768) against a fixed
// exclude; combined with the full fuzz below this pins difference semantics.
func TestSubtractWidth3AllowSubsets(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping allow-subset sweep in -short mode")
	}
	const w = 3
	prefs := allPrefixes(w)
	excludes := [][]struct {
		net int64
		p   int
	}{
		nil,
		{{2, 2}},                 // block [2,4)
		{{0, w}, {7, w}},         // two corner hosts
		{{0, 1}},                 // half space
		{{1, w}, {3, w}, {5, w}}, // scattered
	}
	for _, ex := range excludes {
		for mask := 0; mask < (1 << len(prefs)); mask++ {
			var allow []struct {
				net int64
				p   int
			}
			for i := range prefs {
				if mask&(1<<uint(i)) != 0 {
					allow = append(allow, prefs[i])
				}
			}
			truth, got := evaluateSet(t, allow, ex, w)
			gotSet := rangesToBitset(got, w)
			if gotSet.Cmp(truth) != 0 {
				t.Fatalf("allow=%v exclude=%v\n got=%08b\nwant=%08b", allow, ex, gotSet, truth)
			}
			// Result must itself admit a verified minimal cover.
			blocks := Cover(got, w)
			if v, _ := VerifyCover(blocks, got, w); len(v) != 0 {
				t.Fatalf("verify: %v", v)
			}
		}
	}
}

func rangesToBitset(rs []Range, width int) *big.Int {
	set := new(big.Int)
	for _, r := range rs {
		for x := new(big.Int).Set(r.Start); x.Cmp(r.End) < 0; x.Add(x, big.NewInt(1)) {
			if int(x.Int64()) >= 1<<width {
				break
			}
			set.SetBit(set, int(x.Int64()), 1)
		}
	}
	return set
}

// TestSubtractSinglePrefixPairsWidth5: ALL allow-prefix × exclude-prefix pairs
// at width 5 (63 prefixes each, 3969 combinations).
func TestSubtractSinglePrefixPairsWidth5(t *testing.T) {
	const w = 5
	prefs := allPrefixes(w)
	for _, a := range prefs {
		for _, e := range prefs {
			truth, got := evaluateSet(t,
				[]struct {
					net int64
					p   int
				}{a},
				[]struct {
					net int64
					p   int
				}{e}, w)
			if rangesToBitset(got, w).Cmp(truth) != 0 {
				t.Fatalf("allow=%v exclude=%v mismatch", a, e)
			}
			blocks := Cover(got, w)
			if v, _ := VerifyCover(blocks, got, w); len(v) != 0 {
				t.Fatalf("allow=%v exclude=%v verify: %v", a, e, v)
			}
			if !blocksEqual(blocks, trieOracle(got, w)) {
				t.Fatalf("allow=%v exclude=%v cover != oracle", a, e)
			}
		}
	}
}

// TestSubtractFuzzed: random overlapping allow/exclude prefix sets at widths
// 4..10, including pathological adjacent and duplicate inputs.
func TestSubtractFuzzed(t *testing.T) {
	rng := rand.New(rand.NewSource(20260928))
	for _, w := range []int{4, 5, 6, 8, 10} {
		prefs := allPrefixes(w)
		for iter := 0; iter < 2000; iter++ {
			pick := func() []struct {
				net int64
				p   int
			} {
				k := rng.Intn(8)
				out := make([]struct {
					net int64
					p   int
				}, k)
				for i := range out {
					c := prefs[rng.Intn(len(prefs))]
					out[i] = c
					if rng.Intn(3) == 0 {
						out = append(out, c) // deliberate duplicate
					}
				}
				return out
			}
			allow, exclude := pick(), pick()
			truth, got := evaluateSet(t, allow, exclude, w)
			if rangesToBitset(got, w).Cmp(truth) != 0 {
				t.Fatalf("w=%d allow=%v exclude=%v set mismatch", w, allow, exclude)
			}
			blocks := Cover(got, w)
			if v, _ := VerifyCover(blocks, got, w); len(v) != 0 {
				t.Fatalf("w=%d verify: %v", w, v)
			}
			if !blocksEqual(blocks, trieOracle(got, w)) {
				t.Fatalf("w=%d cover != trie oracle", w)
			}
		}
	}
}

// ---------------------------------------------------------------------------
// Concrete expected results at small widths (specific values, not properties).
// ---------------------------------------------------------------------------

func TestConcreteSmallWidthCovers(t *testing.T) {
	const w = 4
	rng := func(a, b int64) Range { return Range{Start: big.NewInt(a), End: big.NewInt(b)} }
	cases := []struct {
		name   string
		target []Range
		want   []Block
	}{
		{"empty", nil, nil},
		{"full_space", []Range{rng(0, 16)}, []Block{{big.NewInt(0), 0}}},
		{"single_low_address", []Range{rng(0, 1)}, []Block{{big.NewInt(0), 4}}},
		{"single_middle_address", []Range{rng(5, 6)}, []Block{{big.NewInt(5), 4}}},
		{"single_high_address", []Range{rng(15, 16)}, []Block{{big.NewInt(15), 4}}},
		{"two_adjacent", []Range{rng(0, 2)}, []Block{{big.NewInt(0), 3}}},
		{"classic_1_to_8", []Range{rng(1, 8)}, []Block{
			{big.NewInt(1), 4}, {big.NewInt(2), 3}, {big.NewInt(4), 2},
		}},
		{"classic_7_to_15", []Range{rng(7, 15)}, []Block{
			{big.NewInt(7), 4}, {big.NewInt(8), 2}, {big.NewInt(12), 3}, {big.NewInt(14), 4},
		}},
		{"two_runs", []Range{rng(1, 3), rng(12, 16)}, []Block{
			{big.NewInt(1), 4}, {big.NewInt(2), 4}, {big.NewInt(12), 2},
		}},
		{"overlapping_input_ranges_union", []Range{rng(1, 5), rng(3, 8)}, []Block{
			{big.NewInt(1), 4}, {big.NewInt(2), 3}, {big.NewInt(4), 2},
		}},
		{"all_but_corners", []Range{rng(1, 15)}, []Block{
			{big.NewInt(1), 4}, {big.NewInt(2), 3}, {big.NewInt(4), 2},
			{big.NewInt(8), 2}, {big.NewInt(12), 3}, {big.NewInt(14), 4},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Cover(tc.target, w)
			if len(got) != len(tc.want) {
				t.Fatalf("got %v want %v", got, tc.want)
			}
			for i := range got {
				if got[i].PrefixLen != tc.want[i].PrefixLen || got[i].Network.Cmp(tc.want[i].Network) != 0 {
					t.Fatalf("case %s block %d: got {%s/%d} want {%s/%d}\nfull got=%v",
						tc.name, i, got[i].Network, got[i].PrefixLen,
						tc.want[i].Network, tc.want[i].PrefixLen, got)
				}
			}
			if v, _ := VerifyCover(got, Union(tc.target, w), w); len(v) != 0 {
				t.Fatalf("verify: %v", v)
			}
		})
	}
}
