// Package refmodel is an INDEPENDENT reference implementation used only by the
// test suite. It shares NO code with internal/netmodel: it does not import it,
// and its strategy (dense bitsets + top-down trie recursion) is deliberately
// different from the production strategy (big-int intervals + greedy
// largest-block decomposition).
//
// It is exact at reduced bit widths (up to ~24 bits) so the suite can
// exhaustively check:
//
//	no overlap, no gap, no extra address, and strict minimum-cardinality
//
// of the production result — the reference trie output is the unique canonical
// minimum cover, so a mismatch is a definite defect rather than an opinion.
package refmodel

import (
	"fmt"
	"math/bits"
)

// Bitset is a dense set of integers in [0, 2^width). Word 0 holds the lowest
// 64 addresses (bit 0 -> address 0, bit 63 -> address 63); higher words hold
// higher addresses.
type Bitset struct {
	width int
	words []uint64
}

// NewEmpty returns an empty set over a universe of 2^width integers, so the
// backing storage holds 2^width BITS (e.g. width 8 -> 256 bits -> 4 words),
// not `width` bits.
func NewEmpty(width int) *Bitset {
	if width < 0 || width > 24 {
		panic(fmt.Sprintf("refmodel supports widths 0..24, got %d", width))
	}
	// ceil(2^width / 64); 2^(width-6) for width>=6, else one word.
	n := 1
	if width > 6 {
		n = 1 << (width - 6)
	}
	return &Bitset{width: width, words: make([]uint64, n)}
}

// universeMask returns 2^width.
func (b *Bitset) universe() uint64 {
	if b.width >= 64 {
		return 0
	}
	return uint64(1) << b.width
}

// Set marks integer v present.
func (b *Bitset) Set(v uint64) {
	idx := int(v / 64)
	if idx >= len(b.words) {
		panic(fmt.Sprintf("value %d out of width %d", v, b.width))
	}
	b.words[idx] |= uint64(1) << (v % 64)
}

// Has reports membership.
func (b *Bitset) Has(v uint64) bool {
	idx := int(v / 64)
	if idx < 0 || idx >= len(b.words) {
		return false
	}
	return b.words[idx]&(uint64(1)<<(v%64)) != 0
}

// Count returns the population.
func (b *Bitset) Count() uint64 {
	var n uint64
	for _, w := range b.words {
		n += uint64(bits.OnesCount64(w))
	}
	return n
}

// MinPrefix is a reference prefix: base address and prefix length.
type MinPrefix struct {
	Base uint64
	Len  int
}

// blockMask returns the low-h bits mask (h = width-len). A block is full iff
// its address range maps to exactly (2^h) set bits.
func blockStart(base uint64, h int) uint64 { return base &^ ((uint64(1) << h) - 1) }

// CanonicalCover computes the unique minimum prefix cover via top-down binary
// trie recursion: a full block emits one prefix; a partial block descends.
// This algorithm is unrelated to the production greedy decomposition.
func CanonicalCover(width int, set *Bitset) []MinPrefix {
	var out []MinPrefix
	var rec func(base uint64, h int) // block [base, base+2^h), h remaining host bits
	rec = func(base uint64, h int) {
		full, empty := blockState(set, base, h)
		if full {
			out = append(out, MinPrefix{Base: base, Len: width - h})
			return
		}
		if empty {
			return
		}
		// Partial and h==0 cannot occur (a single address is either full/empty).
		half := uint64(1) << (h - 1)
		rec(base, h-1)
		rec(base+half, h-1)
	}
	rec(0, width)
	return out
}

// blockState reports whether the aligned block [base, base+2^h) is fully set /
// fully empty by counting present bits directly.
func blockState(set *Bitset, base uint64, h int) (full, empty bool) {
	size := uint64(1) << h
	end := base + size - 1
	var got uint64
	loWord := int(base / 64)
	hiWord := int(end / 64)
	for wi := loWord; wi <= hiWord; wi++ {
		if wi < 0 || wi >= len(set.words) {
			continue
		}
		boundLo := base
		if wl := uint64(wi) * 64; wl > boundLo {
			boundLo = wl
		}
		boundHi := end
		if wh := uint64(wi)*64 + 63; wh < boundHi {
			boundHi = wh
		}
		bitsN := boundHi - boundLo + 1
		var m uint64
		if bitsN >= 64 {
			m = ^uint64(0)
		} else {
			m = (uint64(1) << bitsN) - 1
		}
		m <<= boundLo % 64
		got += uint64(bits.OnesCount64(set.words[wi] & m))
	}
	return got == size, got == 0
}

// FromIntervals builds a set from closed [lo,hi] intervals by range-filling
// words (it never iterates per-address for large runs; partial ends are
// filled explicitly but only at the two ends).
func FromIntervals(width int, ivs [][2]uint64) *Bitset {
	b := NewEmpty(width)
	for _, iv := range ivs {
		lo, hi := iv[0], iv[1]
		if lo > hi {
			continue
		}
		remaining := hi - lo + 1
		for remaining > 0 {
			if lo%64 == 0 && remaining >= 64 {
				b.words[int(lo/64)] = ^uint64(0)
				lo += 64
				remaining -= 64
				continue
			}
			b.Set(lo)
			lo++
			remaining--
		}
	}
	b.maskTopWord()
	return b
}

// FillBlock sets every address of the aligned block [base, base+2^h). Used by
// tests to reconstruct what a produced prefix claims to cover.
func (b *Bitset) FillBlock(base uint64, h int) {
	remaining := uint64(1) << h
	v := base
	for remaining > 0 {
		if v%64 == 0 && remaining >= 64 {
			b.words[int(v/64)] = ^uint64(0)
			v += 64
			remaining -= 64
			continue
		}
		b.Set(v)
		v++
		remaining--
	}
}

// Invert replaces the set with its complement within the address width.
func (b *Bitset) Invert() *Bitset {
	for i := range b.words {
		b.words[i] = ^b.words[i]
	}
	b.maskTopWord()
	return b
}

// AndNot removes every element of other.
func (b *Bitset) AndNot(other *Bitset) *Bitset {
	for i := range b.words {
		b.words[i] &^= other.words[i]
	}
	return b
}

// Equal compares two same-width sets.
func (b *Bitset) Equal(o *Bitset) bool {
	if b.width != o.width || len(b.words) != len(o.words) {
		return false
	}
	for i := range b.words {
		if b.words[i] != o.words[i] {
			return false
		}
	}
	return true
}

func (b *Bitset) maskTopWord() {
	// The universe is 2^width addresses. The final word only uses
	// (2^width mod 64) low bits, i.e. 2^(width mod 6) bits when width<6...
	// but because word k covers addresses [64k,64k+63] linearly, the valid
	// bits are simply the low (2^width - 64*lastWordIndex) bits.
	last := len(b.words) - 1
	valid := (uint64(1) << b.width) - uint64(last)*64
	if valid >= 64 {
		return
	}
	b.words[last] &= (uint64(1) << valid) - 1
}

// MaskToUniverse clears any bits above 2^width-1. Tests call it once after
// rebuilding a set from produced prefixes via fast word fills.
func (b *Bitset) MaskToUniverse() { b.maskTopWord() }

// Difference returns a \\ b as a fresh set.
func Difference(a, b *Bitset) *Bitset {
	out := NewEmpty(a.width)
	for i := range a.words {
		out.words[i] = a.words[i] &^ b.words[i]
	}
	out.maskTopWord()
	return out
}
