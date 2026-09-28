// Package ippool enumerates the allocatable IPv4 interval and performs
// pure (side-effect-free) address selection. The storage layer decides
// which enumerated address is actually free under its transaction.
package ippool

import (
	"fmt"
	"net/netip"
	"sync/atomic"
)

// Pool is an inclusive [Start, End] IPv4 interval inside one /24-style
// subnet.
type Pool struct {
	start  uint32
	end    uint32
	size   uint32
	cursor atomic.Uint32
}

// New builds a pool from two inclusive IPv4 bounds.
func New(start, end netip.Addr) (*Pool, error) {
	if !start.Is4() || !end.Is4() {
		return nil, fmt.Errorf("ippool: bounds must be IPv4")
	}
	s := addrUint32(start)
	e := addrUint32(end)
	if e < s {
		return nil, fmt.Errorf("ippool: end %s before start %s", end, start)
	}
	p := &Pool{start: s, end: e, size: e - s + 1}
	p.cursor.Store(s)
	return p, nil
}

// Size is the number of addresses in the interval.
func (p *Pool) Size() uint32 { return p.size }

// Start returns the inclusive lower bound.
func (p *Pool) Start() netip.Addr { return uint32Addr(p.start) }

// End returns the inclusive upper bound.
func (p *Pool) End() netip.Addr { return uint32Addr(p.end) }

// Contains reports whether a belongs to the interval.
func (p *Pool) Contains(a netip.Addr) bool {
	if !a.Is4() {
		return false
	}
	v := addrUint32(a)
	return v >= p.start && v <= p.end
}

// Next returns every pool address starting from a rotating cursor, in
// round-robin order, invoking keep for each. Iteration stops and returns
// the first address for which keep returns true; if keep never accepts
// (or the pool is exhausted for this sweep) ok is false. The cursor makes
// concurrent allocations probe disjoint regions first, reducing hot-row
// contention on small pools.
func (p *Pool) Next(keep func(netip.Addr) bool) (netip.Addr, bool) {
	first := p.cursor.Add(1) - 1
	for i := uint32(0); i < p.size; i++ {
		c := p.start + ((first - p.start + i) % p.size)
		a := uint32Addr(c)
		if keep(a) {
			return a, true
		}
	}
	return netip.Addr{}, false
}

// Index returns the zero-based ordinal of a within-pool address.
func (p *Pool) Index(a netip.Addr) (uint32, bool) {
	v := addrUint32(a)
	if v < p.start || v > p.end {
		return 0, false
	}
	return v - p.start, true
}

func addrUint32(a netip.Addr) uint32 {
	b := a.As4()
	return uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
}

func uint32Addr(v uint32) netip.Addr {
	return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
}
