// Package netmodel implements the geometric network model used by the
// first-match analyzer: discrete 1-D interval sets over fixed-width integer
// domains (protocol numbers, ports, IPv4/IPv6 addresses) and products of
// such sets representing packet-class match regions.
//
// All address arithmetic is done with math/big so that IPv4 (32 bit) and
// IPv6 (128 bit) share one implementation; the two families are never mixed
// inside a single Space.
package netmodel

import (
	"fmt"
	"math/big"
	"sort"
)

// Domain widths in bits.
const (
	BitsProto = 8  // IP protocol numbers 0..255
	BitsPort  = 16 // UDP/TCP/SCTP ports 0..65535
	BitsIPv4  = 32
	BitsIPv6  = 128
)

// Seg is an inclusive integer interval [Lo, Hi].
type Seg struct {
	Lo, Hi *big.Int
}

// Int1D is a normalized (sorted, disjoint, adjacent-merged) set of intervals
// over a fixed-width unsigned domain.
type Int1D struct {
	bits int
	segs []Seg
}

// New1D returns an empty set over the given domain width.
func New1D(bits int) Int1D { return Int1D{bits: bits} }

// Universe1D returns the full domain [0, 2^bits-1].
func Universe1D(bits int) Int1D {
	return Int1D{bits: bits, segs: []Seg{{Lo: big.NewInt(0), Hi: domainMax(bits)}}}
}

// Point1D returns the singleton {v}.
func Point1D(bits int, v *big.Int) Int1D {
	return MustRange1D(bits, v, v)
}

// Range1D returns [lo, hi] after validating the bounds against the domain.
func Range1D(bits int, lo, hi *big.Int) (Int1D, error) {
	max := domainMax(bits)
	if lo.Sign() < 0 || hi.Cmp(max) > 0 || lo.Cmp(hi) > 0 {
		return Int1D{}, fmt.Errorf("interval [%v,%v] out of range for %d-bit domain", lo, hi, bits)
	}
	return Int1D{bits: bits, segs: []Seg{{lo, hi}}}, nil
}

// MustRange1D is like Range1D but panics on error; use only with
// compiler-known in-range bounds.
func MustRange1D(bits int, lo, hi *big.Int) Int1D {
	s, err := Range1D(bits, lo, hi)
	if err != nil {
		panic(err)
	}
	return s
}

// FromSegments normalizes an arbitrary segment list (sort, merge overlapping
// or adjacent segments, drop empties) and validates it against the domain.
func FromSegments(bits int, raw []Seg) (Int1D, error) {
	segs := make([]Seg, 0, len(raw))
	max := domainMax(bits)
	for _, s := range raw {
		if s.Lo == nil || s.Hi == nil {
			return Int1D{}, fmt.Errorf("nil interval bound")
		}
		if s.Lo.Sign() < 0 || s.Hi.Cmp(max) > 0 || s.Lo.Cmp(s.Hi) > 0 {
			return Int1D{}, fmt.Errorf("interval [%v,%v] out of range for %d-bit domain", s.Lo, s.Hi, bits)
		}
		segs = append(segs, Seg{new(big.Int).Set(s.Lo), new(big.Int).Set(s.Hi)})
	}
	sort.Slice(segs, func(i, j int) bool { return segs[i].Lo.Cmp(segs[j].Lo) < 0 })
	merged := segs[:0]
	for _, s := range segs {
		if n := len(merged); n > 0 {
			prev := &merged[n-1]
			// adjacent intervals merge: prev.Hi+1 >= s.Lo
			if new(big.Int).Add(prev.Hi, big.NewInt(1)).Cmp(s.Lo) >= 0 {
				if s.Hi.Cmp(prev.Hi) > 0 {
					prev.Hi.Set(s.Hi)
				}
				continue
			}
		}
		merged = append(merged, s)
	}
	return Int1D{bits: bits, segs: merged}, nil
}

func domainMax(bits int) *big.Int {
	return new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), uint(bits)), big.NewInt(1))
}

// Bits reports the domain width.
func (s Int1D) Bits() int { return s.bits }

// IsEmpty reports whether the set contains no values.
func (s Int1D) IsEmpty() bool { return len(s.segs) == 0 }

// Segments returns the normalized segments.
func (s Int1D) Segments() []Seg { return s.segs }

// Contains reports whether v belongs to the set.
func (s Int1D) Contains(v *big.Int) bool {
	i := sort.Search(len(s.segs), func(i int) bool { return s.segs[i].Hi.Cmp(v) >= 0 })
	return i < len(s.segs) && v.Cmp(s.segs[i].Lo) >= 0
}

// Min returns the smallest member, or nil for the empty set.
func (s Int1D) Min() *big.Int {
	if len(s.segs) == 0 {
		return nil
	}
	return new(big.Int).Set(s.segs[0].Lo)
}

// Equals compares two sets as sets (independent of construction history).
func (s Int1D) Equals(o Int1D) bool {
	if s.bits != o.bits || len(s.segs) != len(o.segs) {
		return false
	}
	for i := range s.segs {
		if s.segs[i].Lo.Cmp(o.segs[i].Lo) != 0 || s.segs[i].Hi.Cmp(o.segs[i].Hi) != 0 {
			return false
		}
	}
	return true
}

// Subset reports whether s is contained in o.
func (s Int1D) Subset(o Int1D) bool {
	return s.Minus(o).IsEmpty()
}

// Intersect returns the set intersection.
func (s Int1D) Intersect(o Int1D) Int1D {
	if s.bits != o.bits {
		panic(fmt.Sprintf("domain mismatch: %d vs %d", s.bits, o.bits))
	}
	var out []Seg
	for _, a := range s.segs {
		for _, b := range o.segs {
			if b.Hi.Cmp(a.Lo) < 0 {
				continue
			}
			if b.Lo.Cmp(a.Hi) > 0 {
				break
			}
			lo := maxBig(a.Lo, b.Lo)
			hi := minBig(a.Hi, b.Hi)
			if lo.Cmp(hi) <= 0 {
				out = append(out, Seg{lo, hi})
			}
		}
	}
	return Int1D{bits: s.bits, segs: out}
}

// Minus returns the set difference s \ o.
func (s Int1D) Minus(o Int1D) Int1D {
	if s.bits != o.bits {
		panic(fmt.Sprintf("domain mismatch: %d vs %d", s.bits, o.bits))
	}
	if len(s.segs) == 0 || len(o.segs) == 0 {
		return Int1D{bits: s.bits, segs: append([]Seg(nil), s.segs...)}
	}
	var out []Seg
	for _, a := range s.segs {
		cur := new(big.Int).Set(a.Lo)
		for _, b := range o.segs {
			if b.Hi.Cmp(a.Lo) < 0 {
				continue // b entirely before a
			}
			if b.Lo.Cmp(a.Hi) > 0 {
				break // b entirely after a; lists sorted
			}
			lo := maxBig(b.Lo, a.Lo)
			hi := minBig(b.Hi, a.Hi)
			if lo.Cmp(cur) > 0 {
				out = append(out, Seg{new(big.Int).Set(cur), new(big.Int).Sub(lo, big.NewInt(1))})
			}
			if nb := new(big.Int).Add(hi, big.NewInt(1)); nb.Cmp(cur) > 0 {
				cur = nb
			}
			if cur.Cmp(a.Hi) > 0 {
				break
			}
		}
		if cur.Cmp(a.Hi) <= 0 {
			out = append(out, Seg{new(big.Int).Set(cur), new(big.Int).Set(a.Hi)})
		}
	}
	return Int1D{bits: s.bits, segs: out}
}

func maxBig(a, b *big.Int) *big.Int {
	if a.Cmp(b) >= 0 {
		return new(big.Int).Set(a)
	}
	return new(big.Int).Set(b)
}

func minBig(a, b *big.Int) *big.Int {
	if a.Cmp(b) <= 0 {
		return new(big.Int).Set(a)
	}
	return new(big.Int).Set(b)
}
