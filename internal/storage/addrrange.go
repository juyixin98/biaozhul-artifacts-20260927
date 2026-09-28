package storage

import (
	"net/netip"
)

// AddrRange enumerates an inclusive IPv4 interval as uint32 host order.
type AddrRange struct {
	start uint32
	end   uint32
}

// NewAddrRange validates and builds an inclusive pool range.
func NewAddrRange(start, end netip.Addr) (AddrRange, error) {
	s := addrUint(start)
	e := addrUint(end)
	if s > e {
		return AddrRange{}, &RangeError{Start: start.String(), End: end.String()}
	}
	return AddrRange{start: s, end: e}, nil
}

// RangeError indicates an inverted range.
type RangeError struct{ Start, End string }

func (e *RangeError) Error() string { return "inverted range " + e.Start + " > " + e.End }

// Contains reports whether a belongs to the range.
func (r AddrRange) Contains(a netip.Addr) bool {
	v := addrUint(a)
	return v >= r.start && v <= r.end
}

// Size returns the inclusive number of addresses.
func (r AddrRange) Size() uint64 { return uint64(r.end) - uint64(r.start) + 1 }

// AddrAt returns the n-th address (zero based).
func (r AddrRange) AddrAt(n uint64) netip.Addr {
	return uintAddr(r.start + uint32(n))
}

// Ascending returns up to limit addresses starting from the pool bottom,
// used by candidate scans in stable test fixtures.
func (r AddrRange) Ascending(limit int) []netip.Addr {
	out := make([]netip.Addr, 0, limit)
	for v := r.start; v <= r.end && len(out) < limit; v++ {
		out = append(out, uintAddr(v))
		if v == ^uint32(0) { // avoid wrap overflow
			break
		}
	}
	return out
}

func addrUint(a netip.Addr) uint32 {
	a = a.Unmap()
	b := a.As4()
	return uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
}

func uintAddr(v uint32) netip.Addr {
	return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
}
