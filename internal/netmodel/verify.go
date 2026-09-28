package netmodel

import (
	"fmt"
	"math/big"
	"sort"
)

// VerificationCategory classifies a failed verification so callers and tests
// can assert the specific failure mode instead of a generic boolean.
type VerificationCategory string

const (
	VerifyOverlap          VerificationCategory = "overlap"
	VerifyGap              VerificationCategory = "missing_address"
	VerifyExtraAddress     VerificationCategory = "extra_address"
	VerifyOutOfUniverse    VerificationCategory = "out_of_universe"
	VerifyMergeableSibling VerificationCategory = "mergeable_sibling"
	VerifyMisalignedBlock  VerificationCategory = "misaligned_block"
	VerifyDuplicateBlock   VerificationCategory = "duplicate_block"
	VerifyInvalidPrefixLen VerificationCategory = "invalid_prefix_length"
)

// Violation describes one concrete verification failure.
type Violation struct {
	Category VerificationCategory
	Detail   string
}

func (v Violation) String() string { return string(v.Category) + ": " + v.Detail }

// Proof is the machine-checkable evidence returned alongside a cover.
type Proof struct {
	Width              int      `json:"width"`
	TargetAddressCount string   `json:"target_address_count"`
	CoverAddressCount  string   `json:"cover_address_count"`
	BlockCount         int      `json:"block_count"`
	Overlaps           []string `json:"overlaps,omitempty"`
	Equivalent         bool     `json:"exactly_equivalent"`
	SiblingMerges      []string `json:"mergeable_siblings,omitempty"`
}

// VerifyCover independently reconstructs the address set implied by blocks and
// compares it with the independently computed target ranges. It does not call
// Subtract or Cover: ranges are rebuilt straight from big.Int arithmetic and
// normalised with a local sweep, so a shared implementation bug cannot mask
// itself.
//
// The checks are:
//  1. structural: prefix lengths legal, blocks aligned, no duplicates;
//  2. no overlap between any two blocks;
//  3. exact set equivalence cover == target (no gap, no extra address);
//  4. no pair of equal-length sibling blocks remains that could merge.
func VerifyCover(blocks []Block, target []Range, width int) ([]Violation, Proof) {
	var violations []Violation
	uni := Universe(width)

	// --- structural validation + range reconstruction ---------------------
	ranges := make([]Range, 0, len(blocks))
	seen := make(map[string]bool, len(blocks))
	for _, b := range blocks {
		if b.PrefixLen < 0 || b.PrefixLen > width {
			violations = append(violations, Violation{
				Category: VerifyInvalidPrefixLen,
				Detail:   fmt.Sprintf("network=%s prefixLen=%d width=%d", b.Network, b.PrefixLen, width),
			})
			continue
		}
		size := new(big.Int).Lsh(big.NewInt(1), uint(width-b.PrefixLen))
		// Alignment: network must be a multiple of the block size.
		if rem := new(big.Int).Mod(b.Network, size); rem.Sign() != 0 {
			violations = append(violations, Violation{
				Category: VerifyMisalignedBlock,
				Detail:   fmt.Sprintf("network=%s not aligned to /%d", b.Network, b.PrefixLen),
			})
		}
		end := new(big.Int).Add(b.Network, size)
		if b.Network.Cmp(uni.Start) < 0 || end.Cmp(uni.End) > 0 {
			violations = append(violations, Violation{
				Category: VerifyOutOfUniverse,
				Detail:   fmt.Sprintf("%s/%d reaches outside %d-bit space", b.Network, b.PrefixLen, width),
			})
		}
		key := b.Network.String() + "/" + fmt.Sprint(b.PrefixLen)
		if seen[key] {
			violations = append(violations, Violation{
				Category: VerifyDuplicateBlock,
				Detail:   key,
			})
		}
		seen[key] = true
		ranges = append(ranges, Range{Start: new(big.Int).Set(b.Network), End: end})
	}

	// --- overlap detection ------------------------------------------------
	coverNorm := localNormalize(ranges)
	overlapPairs := findOverlaps(ranges)
	for _, p := range overlapPairs {
		violations = append(violations, Violation{Category: VerifyOverlap, Detail: p})
	}

	// --- exact equivalence -------------------------------------------------
	targetNorm := localNormalize(cloneRanges(target))
	gaps, extras := symmetricDifference(coverNorm, targetNorm)
	for _, g := range gaps {
		violations = append(violations, Violation{
			Category: VerifyGap,
			Detail:   fmt.Sprintf("target addresses absent from cover: %s", g),
		})
	}
	for _, x := range extras {
		violations = append(violations, Violation{
			Category: VerifyExtraAddress,
			Detail:   fmt.Sprintf("cover contains addresses not in target: %s", x),
		})
	}

	// --- sibling merge check ----------------------------------------------
	merges := findSiblingMerges(blocks, width)
	for _, m := range merges {
		violations = append(violations, Violation{Category: VerifyMergeableSibling, Detail: m})
	}

	proof := Proof{
		Width:              width,
		TargetAddressCount: Size(targetNorm).String(),
		CoverAddressCount:  Size(coverNorm).String(),
		BlockCount:         len(blocks),
		Equivalent:         len(gaps) == 0 && len(extras) == 0,
	}
	for _, p := range overlapPairs {
		proof.Overlaps = append(proof.Overlaps, p)
	}
	proof.SiblingMerges = merges
	return violations, proof
}

func cloneRanges(rs []Range) []Range {
	out := make([]Range, len(rs))
	for i, r := range rs {
		out[i] = Range{Start: new(big.Int).Set(r.Start), End: new(big.Int).Set(r.End)}
	}
	return out
}

// localNormalize is a private sort+merge used only by verification, kept
// separate from the production Union on purpose.
func localNormalize(rs []Range) []Range {
	if len(rs) == 0 {
		return nil
	}
	sorted := cloneRanges(rs)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].Start.Cmp(sorted[j].Start) < 0
	})
	out := []Range{sorted[0]}
	for _, r := range sorted[1:] {
		last := &out[len(out)-1]
		if r.Start.Cmp(last.End) <= 0 {
			if r.End.Cmp(last.End) > 0 {
				last.End.Set(r.End)
			}
		} else {
			out = append(out, r)
		}
	}
	return out
}

func findOverlaps(rs []Range) []string {
	var out []string
	for i := 0; i < len(rs); i++ {
		for j := i + 1; j < len(rs); j++ {
			// Half-open overlap: max(starts) < min(ends).
			lo := new(big.Int)
			if rs[i].Start.Cmp(rs[j].Start) > 0 {
				lo.Set(rs[i].Start)
			} else {
				lo.Set(rs[j].Start)
			}
			hi := new(big.Int)
			if rs[i].End.Cmp(rs[j].End) < 0 {
				hi.Set(rs[i].End)
			} else {
				hi.Set(rs[j].End)
			}
			if lo.Cmp(hi) < 0 {
				out = append(out, fmt.Sprintf("%s overlaps %s on %s", rs[i], rs[j], Range{Start: lo, End: hi}))
			}
		}
	}
	return out
}

// symmetricDifference returns (addresses in B not A, addresses in A not B)
// for two normalised sets.
func symmetricDifference(a, b []Range) (inBnotA, inAnotB []Range) {
	inBnotA = diffNormalized(b, a)
	inAnotB = diffNormalized(a, b)
	return
}

// diffNormalized returns normalized X \\ Y from two normalised sets.
func diffNormalized(x, y []Range) []Range {
	var out []Range
	for _, rx := range x {
		cursor := new(big.Int).Set(rx.Start)
		for _, ry := range y {
			if ry.End.Cmp(cursor) <= 0 {
				continue
			}
			if ry.Start.Cmp(rx.End) >= 0 {
				break
			}
			if ry.Start.Cmp(cursor) > 0 {
				out = append(out, Range{Start: new(big.Int).Set(cursor), End: new(big.Int).Set(ry.Start)})
			}
			if ry.End.Cmp(cursor) > 0 {
				cursor.Set(ry.End)
			}
			if cursor.Cmp(rx.End) >= 0 {
				break
			}
		}
		if cursor.Cmp(rx.End) < 0 {
			out = append(out, Range{Start: new(big.Int).Set(cursor), End: new(big.Int).Set(rx.End)})
		}
	}
	return localNormalize(out)
}

// findSiblingMerges reports every block whose equal-length sibling (the block
// obtained by flipping the bit at position width-prefixLen) is also present.
// The pair can then be replaced by their parent, so its existence disproves
// minimality. A block at /0 has no sibling and is skipped.
func findSiblingMerges(blocks []Block, width int) []string {
	type key struct {
		net       string
		prefixLen int
	}
	present := make(map[key]bool, len(blocks))
	for _, b := range blocks {
		present[key{b.Network.String(), b.PrefixLen}] = true
	}
	var merges []string
	emitted := make(map[string]bool)
	for _, b := range blocks {
		if b.PrefixLen == 0 || b.PrefixLen > width {
			continue
		}
		bit := uint(width - b.PrefixLen)
		sibling := new(big.Int).Set(b.Network)
		sibling.Xor(sibling, new(big.Int).Lsh(big.NewInt(1), bit))
		sk := key{sibling.String(), b.PrefixLen}
		if present[sk] {
			lo, hi := b.Network, sibling
			if lo.Cmp(hi) > 0 {
				lo, hi = hi, lo
			}
			parent := new(big.Int).Set(lo)
			desc := fmt.Sprintf("%s/%d and %s/%d merge into parent at /%d (network=%s)",
				lo, b.PrefixLen, hi, b.PrefixLen, b.PrefixLen-1, parent)
			// Deduplicate: each mergeable pair seen twice.
			pairKey := lo.String() + "/" + fmt.Sprint(b.PrefixLen)
			if !emitted[pairKey] {
				emitted[pairKey] = true
				merges = append(merges, desc)
			}
		}
	}
	return merges
}
