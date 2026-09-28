// Package netmodel defines the packet-space model used for first-match
// firewall semantics: addresses, CIDR blocks, protocols, port intervals and
// their partition/intersection algebra.
//
// Everything here is pure geometry over packet coordinates; rule ordering and
// default actions live in the analyzer package.
package netmodel

import (
	"fmt"
	"math/big"
	"net/netip"
)

// Family is an IP version. All address-space algebra is performed separately
// per family; an IPv4 box never intersects an IPv6 box.
type Family uint8

const (
	FamUnknown Family = 0
	FamV4      Family = 4
	FamV6      Family = 6
)

func (f Family) String() string {
	switch f {
	case FamV4:
		return "ipv4"
	case FamV6:
		return "ipv6"
	default:
		return "unknown"
	}
}

// Bits returns the total address bits of the family (32 / 128).
func (f Family) Bits() int {
	switch f {
	case FamV4:
		return 32
	case FamV6:
		return 128
	default:
		return 0
	}
}

// Addr is a fixed-size numeric address. For IPv4 only L holds the 32-bit
// value (H == 0); for IPv6 H holds the high 64 bits and L the low 64 bits.
type Addr struct {
	H uint64
	L uint64
}

// AddrFromNetIP converts a netip.Addr to the numeric representation.
func AddrFromNetIP(ip netip.Addr) (Addr, Family) {
	ip = ip.Unmap()
	if ip.Is4() {
		b := ip.As4()
		var v uint64
		for _, x := range b {
			v = (v << 8) | uint64(x)
		}
		return Addr{L: v}, FamV4
	}
	b := ip.As16()
	var h, l uint64
	for i := 0; i < 8; i++ {
		h = (h << 8) | uint64(b[i])
		l = (l << 8) | uint64(b[i+8])
	}
	return Addr{H: h, L: l}, FamV6
}

// ParseAddr parses "1.2.3.4" or "2001:db8::1".
func ParseAddr(s string) (Addr, Family, error) {
	ip, err := netip.ParseAddr(s)
	if err != nil {
		return Addr{}, FamUnknown, err
	}
	a, f := AddrFromNetIP(ip)
	return a, f, nil
}

// NetIP converts back to netip.Addr.
func (a Addr) NetIP(f Family) netip.Addr {
	if f == FamV4 {
		b := [4]byte{
			byte(a.L >> 24), byte(a.L >> 16), byte(a.L >> 8), byte(a.L),
		}
		return netip.AddrFrom4(b)
	}
	var b [16]byte
	for i := 0; i < 8; i++ {
		b[i] = byte(a.H >> (56 - 8*uint(i)))
		b[i+8] = byte(a.L >> (56 - 8*uint(i)))
	}
	return netip.AddrFrom16(b)
}

func (a Addr) String(f Family) string { return a.NetIP(f).String() }

// withLowBitsSet returns the address having the same high bits as a and its
// low n bits set to 1 (n may be 0..128).
func withLowBitsSet(a Addr, n int) Addr {
	switch {
	case n <= 0:
		return a
	case n >= 128:
		return Addr{H: ^uint64(0), L: ^uint64(0)}
	case n < 64:
		a.L |= (uint64(1) << uint(n)) - 1
		return a
	default:
		a.H |= (uint64(1) << uint(n-64)) - 1
		a.L = ^uint64(0)
		return a
	}
}

// CIDR is an aligned network block [First, Last] of one family.
type CIDR struct {
	Fam    Family
	Prefix int
	First  Addr
	Last   Addr
}

// newCIDR builds a block from its aligned first address and prefix length.
func newCIDR(fam Family, first Addr, prefix int) CIDR {
	return CIDR{
		Fam:    fam,
		Prefix: prefix,
		First:  first,
		Last:   withLowBitsSet(first, fam.Bits()-prefix),
	}
}

// RootBlock returns 0.0.0.0/0 or ::/0.
func RootBlock(fam Family) CIDR { return newCIDR(fam, Addr{}, 0) }

// ParseCIDR parses an a.b.c.d/nn or v6/nn literal.
func ParseCIDR(s string) (CIDR, error) {
	p, err := netip.ParsePrefix(s)
	if err != nil {
		return CIDR{}, err
	}
	p = p.Masked()
	fam := FamV6
	if p.Addr().Unmap().Is4() {
		fam = FamV4
	}
	first, _ := AddrFromNetIP(p.Addr().Unmap())
	return newCIDR(fam, first, p.Bits()), nil
}

// String renders the canonical CIDR literal.
func (c CIDR) String() string {
	return fmt.Sprintf("%s/%d", c.First.NetIP(c.Fam).String(), c.Prefix)
}

// Contains reports whether ip falls inside the block.
func (c CIDR) Contains(ip Addr) bool {
	return geAddr(ip, c.First) && leAddr(ip, c.Last)
}

// Size returns the number of addresses in the block (2^(bits-prefix)).
func (c CIDR) Size() *big.Int {
	host := c.Fam.Bits() - c.Prefix
	return new(big.Int).Lsh(big.NewInt(1), uint(host))
}

// children returns the two /(prefix+1) children: zero-bit branch then one-bit.
func (c CIDR) children() (CIDR, CIDR) {
	host := c.Fam.Bits() - c.Prefix - 1 // host bits of each child
	oneFirst := withLowBitsSet(c.First, 0)
	oneFirst = setBitFromMSB(oneFirst, c.Fam, c.Prefix)
	zero := newCIDR(c.Fam, c.First, c.Prefix+1)
	one := newCIDR(c.Fam, oneFirst, c.Prefix+1)
	_ = host
	return zero, one
}

// setBitFromMSB sets bit at position p counted from the MSB (0-based).
func setBitFromMSB(a Addr, fam Family, p int) Addr {
	if p < 64 {
		if fam == FamV4 {
			a.L |= uint64(1) << uint(fam.Bits()-1-p)
			return a
		}
		a.H |= uint64(1) << uint(63-p)
		return a
	}
	a.L |= uint64(1) << uint(fam.Bits()-1-p)
	return a
}

func geAddr(a, b Addr) bool { return a.H > b.H || a.H == b.H && a.L >= b.L }
func leAddr(a, b Addr) bool { return a.H < b.H || a.H == b.H && a.L <= b.L }

func sortCIDRs(cs []CIDR) {
	// insertion sort — lists are small (rule counts), avoids extra deps.
	for i := 1; i < len(cs); i++ {
		for j := i; j > 0 && lessCIDR(cs[j], cs[j-1]); j-- {
			cs[j], cs[j-1] = cs[j-1], cs[j]
		}
	}
}

func lessCIDR(a, b CIDR) bool {
	if a.First != b.First {
		return geAddr(b.First, a.First) && a.First != b.First
	}
	return a.Prefix > b.Prefix
}

// containsCIDR reports whether outer fully covers inner.
func containsCIDR(outer, inner CIDR) bool {
	if outer.Fam != inner.Fam {
		return false
	}
	return geAddr(inner.First, outer.First) && leAddr(inner.Last, outer.Last)
}

// PartitionCIDRs returns a set of disjoint, aligned blocks covering exactly
// the union of the inputs, with the stronger property required by analysis:
// every output block is homogeneous for the whole input vector, i.e. each
// input block either fully contains the output block or is disjoint from it.
// Blocks thus never straddle an input boundary, so one first-match result per
// block decides the entire block.
//
// This is exact geometry, not text-prefix comparison: overlapping blocks
// (e.g. 10.0.0.0/23 and 10.0.1.0/24, or a containing /8 plus an inner /16)
// are split at real bit boundaries until every boundary aligns.
func PartitionCIDRs(blocks []CIDR) []CIDR {
	// Dedup only; contained blocks MUST be retained because their boundaries
	// refine the partition (otherwise a containing mark would swallow them).
	uniq := make([]CIDR, 0, len(blocks))
	for _, b := range blocks {
		dup := false
		for _, u := range uniq {
			if u == b {
				dup = true
				break
			}
		}
		if !dup {
			uniq = append(uniq, b)
		}
	}
	if len(uniq) == 0 {
		return nil
	}
	fam := uniq[0].Fam
	var out []CIDR
	var walk func(c CIDR)
	walk = func(c CIDR) {
		intersects := false
		crossing := false // some input block's boundary cuts through c
		for _, b := range uniq {
			ov := leAddr(c.First, b.Last) && leAddr(b.First, c.Last)
			if !ov {
				continue
			}
			intersects = true
			if !containsCIDR(b, c) {
				crossing = true
			}
		}
		if !intersects {
			return
		}
		// Uniform node: every intersecting block fully contains c.
		if !crossing {
			out = append(out, c)
			return
		}
		if c.Prefix >= fam.Bits() {
			// Single address inside an intersecting (hence containing) block.
			out = append(out, c)
			return
		}
		z, o := c.children()
		walk(z)
		walk(o)
	}
	walk(RootBlock(fam))
	sortCIDRs(out)
	return out
}
