package netmodel

import (
	"math/big"
	"net/netip"
)

// Block is a width-parameterised CIDR block: the network ordinal and the
// prefix length. It is family-agnostic, which lets the exhaustive reference
// tests exercise the identical algorithm at widths 3..7.
type Block struct {
	Network   *big.Int
	PrefixLen int
}

// MaxPrefixLen is the largest legal prefix length at which a block aligned at
// Network may start without over-running [start, end).
//
// At prefix length p the block size is 2^(width-p); feasibility needs
//
//	Network mod 2^(width-p) == 0          (alignment)
//	Network + 2^(width-p) <= End          (containment)
//
// We take the largest feasible p-shortest block, i.e. the one with the most
// trailing zero bits that still fits. The greedy choice is safe: any aligned
// block starting at Network covering the first address also covers a prefix of
// the feasible region, and choosing the longest merely leaves the remainder to
// the next iteration, never producing overlap. Iterating left to right yields
// the unique minimal block decomposition (see docs/ALGORITHM.md for the proof
// of minimum count and the sibling-merge argument).
func MaxPrefixLen(start, end *big.Int, width int) int {
	remaining := new(big.Int).Sub(end, start)
	if remaining.Sign() <= 0 {
		return width // empty: single-address /32 or /128 convention never emitted
	}
	// Largest power of two dividing start (trailing zero bits), capped at width
	// so start==0 maps to the full-width alignment rather than "infinite".
	align := width
	if start.Sign() != 0 {
		if tz := trailingZeros(start); tz < width {
			align = tz
		}
	}
	// Largest power of two not exceeding the remaining length:
	// minExponent = floor(log2(remaining)).
	minExp := remaining.BitLen() - 1
	exp := align
	if minExp < exp {
		exp = minExp
	}
	return width - exp
}

// trailingZeros returns the number of low-order zero bits of a positive int.
func trailingZeros(n *big.Int) int {
	tz := 0
	for b := 0; b < n.BitLen(); b++ {
		if n.Bit(b) == 1 {
			break
		}
		tz++
	}
	return tz
}

// Cover decomposes ranges into the minimal list of non-overlapping, gap-free
// CIDR blocks covering exactly those ranges. Inputs are defensively normalised
// (union of overlapping and adjacent ranges, clamp to the universe), so callers
// may pass raw intervals.
func Cover(ranges []Range, width int) []Block {
	ranges = Union(ranges, width)
	blocks := make([]Block, 0)
	for _, r := range ranges {
		cursor := new(big.Int).Set(r.Start)
		for cursor.Cmp(r.End) < 0 {
			pLen := MaxPrefixLen(cursor, r.End, width)
			blocks = append(blocks, Block{
				Network:   new(big.Int).Set(cursor),
				PrefixLen: pLen,
			})
			size := new(big.Int).Lsh(big.NewInt(1), uint(width-pLen))
			cursor.Add(cursor, size)
		}
	}
	return blocks
}

// ToPrefix converts a width-32/128 block into a canonical Prefix.
func ToPrefix(b Block, width int) (Prefix, error) {
	if width != IPv4Bits && width != IPv6Bits {
		return Prefix{}, &PrefixError{
			Kind:  KindMalformed,
			Input: b.Network.String(),
			msg:   "ToPrefix only supports widths 32 and 128",
		}
	}
	addr, ok := ordinalToAddr(b.Network, width)
	if !ok {
		return Prefix{}, &PrefixError{
			Kind:  KindMalformed,
			Input: b.Network.String(),
			msg:   "ordinal outside address space",
		}
	}
	pp, err := addr.Prefix(b.PrefixLen)
	if err != nil {
		return Prefix{}, &PrefixError{Kind: KindPrefixTooLong, Input: b.Network.String(), msg: err.Error()}
	}
	return Prefix{addr: pp.Masked().Addr(), prefix: uint8(pp.Bits())}, nil
}

func ordinalToAddr(n *big.Int, width int) (netip.Addr, bool) {
	uni := Universe(width)
	if n.Cmp(uni.Start) < 0 || n.Cmp(uni.End) >= 0 {
		return netip.Addr{}, false
	}
	buf := make([]byte, width/8)
	n.FillBytes(buf)
	if width == IPv4Bits {
		var a [4]byte
		copy(a[:], buf)
		return netip.AddrFrom4(a), true
	}
	var a [16]byte
	copy(a[:], buf)
	return netip.AddrFrom16(a), true
}

// CoverPrefixes runs the full pipeline for a real address family: allow minus
// exclude, expressed as canonical prefixes.
func CoverPrefixes(allow, exclude []Prefix, width int) ([]Prefix, error) {
	for _, p := range allow {
		if p.FamilyBits() != width {
			return nil, &PrefixError{Kind: KindFamilyMismatch, Input: p.String(), msg: "mixed address families in request"}
		}
	}
	for _, p := range exclude {
		if p.FamilyBits() != width {
			return nil, &PrefixError{Kind: KindFamilyMismatch, Input: p.String(), msg: "mixed address families in request"}
		}
	}
	ar := make([]Range, len(allow))
	for i, p := range allow {
		ar[i] = PrefixRange(p)
	}
	er := make([]Range, len(exclude))
	for i, p := range exclude {
		er[i] = PrefixRange(p)
	}
	diff := Subtract(ar, er, width)
	out := make([]Prefix, 0, len(diff))
	for _, blk := range Cover(diff, width) {
		p, err := ToPrefix(blk, width)
		if err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, nil
}

// BlockRange returns the half-open range [start,end) of a block.
func BlockRange(b Block, width int) Range {
	start := new(big.Int).Set(b.Network)
	size := new(big.Int).Lsh(big.NewInt(1), uint(width-b.PrefixLen))
	return Range{Start: start, End: new(big.Int).Add(start, size)}
}
