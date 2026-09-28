package netmodel

import (
	"math/big"
	"testing"
)

// TestVerificationFailureCategories mutates otherwise-valid covers and asserts
// VerifyCover reports the precise category. This proves the checker catches
// real defects rather than rubber-stamping its sibling code.
func TestVerificationFailureCategories(t *testing.T) {
	const w = 4
	target := []Range{{Start: big.NewInt(1), End: big.NewInt(8)}} // {1/4},{2/3},{4/2}
	good := Cover(target, w)
	if len(good) != 3 {
		t.Fatalf("test setup: expected 3 blocks, got %v", good)
	}

	cases := []struct {
		name    string
		mutate  func([]Block) []Block
		wantCat VerificationCategory
	}{
		{
			name: "overlap",
			mutate: func(b []Block) []Block {
				// duplicate the first block
				return append(b, b[0])
			},
			wantCat: VerifyDuplicateBlock,
		},
		{
			name: "gap missing address",
			mutate: func(b []Block) []Block {
				// drop first block -> address 1 missing
				return b[1:]
			},
			wantCat: VerifyGap,
		},
		{
			name: "extra address",
			mutate: func(b []Block) []Block {
				// append a block above the target
				return append(b, Block{Network: big.NewInt(12), PrefixLen: 3})
			},
			wantCat: VerifyExtraAddress,
		},
		{
			name: "misaligned block",
			mutate: func(b []Block) []Block {
				// the /31 [2,4) becomes an unaligned /31 starting at 3
				out := append([]Block{}, b...)
				out[1] = Block{Network: big.NewInt(3), PrefixLen: 3}
				return out
			},
			wantCat: VerifyMisalignedBlock,
		},
		{
			name: "mergeable siblings present",
			mutate: func(b []Block) []Block {
				// Replace the aligned /2 [4,8) with two mergeable /3 siblings.
				out := append([]Block{}, b[:2]...)
				out = append(out,
					Block{Network: big.NewInt(4), PrefixLen: 3},
					Block{Network: big.NewInt(6), PrefixLen: 3})
				return out
			},
			wantCat: VerifyMergeableSibling,
		},
		{
			name: "invalid prefix length",
			mutate: func(b []Block) []Block {
				return append(b, Block{Network: big.NewInt(0), PrefixLen: 99})
			},
			wantCat: VerifyInvalidPrefixLen,
		},
		{
			name: "out of universe",
			mutate: func(b []Block) []Block {
				return []Block{{Network: big.NewInt(16), PrefixLen: 4}}
			},
			wantCat: VerifyOutOfUniverse,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			bad := c.mutate(append([]Block{}, good...))
			violations, _ := VerifyCover(bad, target, w)
			var found bool
			for _, v := range violations {
				if v.Category == c.wantCat {
					found = true
				}
			}
			if !found {
				t.Fatalf("expected category %s, got violations: %v", c.wantCat, violations)
			}
		})
	}
}

// TestVerifyCleanCoverHasNoViolations is the positive control at several
// widths including the real families.
func TestVerifyCleanCoverHasNoViolations(t *testing.T) {
	for _, w := range []int{3, 4, 5, 8, 32, 128} {
		uni := []Range{Universe(w)}
		b := Cover(uni, w)
		if v, proof := VerifyCover(b, uni, w); len(v) != 0 {
			t.Fatalf("w=%d violations=%v proof=%+v", w, v, proof)
		}
	}
}
