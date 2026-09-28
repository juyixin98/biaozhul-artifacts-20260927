package oracle

import (
	"encoding/binary"
	"net/netip"
)

// ExpandCIDR enumerates every host address in the prefix. Test fixtures use
// tiny prefixes (/30, /31, /126, /127) so this stays small.
func ExpandCIDR(cidr string) []string {
	pfx := netip.MustParsePrefix(cidr)
	var out []string
	if pfx.Addr().Is4() {
		base := binary.BigEndian.Uint32(pfx.Masked().Addr().AsSlice())
		hosts := uint64(1) << uint(32-pfx.Bits())
		for i := uint64(0); i < hosts; i++ {
			v := base + uint32(i)
			var b [4]byte
			binary.BigEndian.PutUint32(b[:], v)
			out = append(out, netip.AddrFrom4(b).String())
		}
	} else {
		base := pfx.Masked().Addr().As16()
		start := new(big128).setBytes(base[:])
		hosts := new(big128).lsh(1, uint(128-pfx.Bits()))
		for i := new(big128); i.cmp(hosts) < 0; i.add(i, one128) {
			v := new(big128).add(start, i)
			var arr [16]byte
			copy(arr[:], v.bytes())
			out = append(out, netip.AddrFrom16(arr).Unmap().String())
		}
	}
	return out
}

// Universe describes the finite enumeration domain for a scenario.
type Universe struct {
	Family   string
	Protos   []int
	SrcAddrs []string
	DstAddrs []string
	SrcPorts []int
	DstPorts []int
}

// Addrs collects host addresses across several CIDRs, de-duplicated and
// sorted by value.
func Addrs(family string, cidrs ...string) []string {
	seen := map[string]bool{}
	var out []string
	for _, c := range cidrs {
		for _, a := range ExpandCIDR(c) {
			af := netip.MustParseAddr(a)
			want4 := family == "ipv4"
			if af.Is4() != want4 {
				continue
			}
			if !seen[a] {
				seen[a] = true
				out = append(out, a)
			}
		}
	}
	return out
}

// Enumerate returns the full cartesian product of the universe.
func Enumerate(u Universe) []Pk {
	var out []Pk
	for _, pr := range u.Protos {
		for _, s := range u.SrcAddrs {
			for _, d := range u.DstAddrs {
				for _, sp := range u.SrcPorts {
					for _, dp := range u.DstPorts {
						out = append(out, Pk{Family: u.Family, Proto: pr, Src: s, Dst: d, SrcPort: sp, DstPort: dp})
					}
				}
			}
		}
	}
	return out
}

// Ports expands an inclusive range for a small test universe.
func Ports(lo, hi int) []int {
	out := make([]int, 0, hi-lo+1)
	for p := lo; p <= hi; p++ {
		out = append(out, p)
	}
	return out
}

// --- tiny uint128 arithmetic (stdlib lacks one), only for IPv6 enumeration ---

type big128 struct{ hi, lo uint64 }

var one128 = &big128{lo: 1}

func (z *big128) setBytes(b []byte) *big128 {
	z.hi = binary.BigEndian.Uint64(b[:8])
	z.lo = binary.BigEndian.Uint64(b[8:])
	return z
}
func (z *big128) bytes() []byte {
	var b [16]byte
	binary.BigEndian.PutUint64(b[:8], z.hi)
	binary.BigEndian.PutUint64(b[8:], z.lo)
	return b[:]
}
func (z *big128) lsh(v uint64, n uint) *big128 {
	switch {
	case n == 0:
		z.hi, z.lo = 0, v
	case n >= 64:
		z.hi, z.lo = v<<(n-64), 0
	default:
		z.hi, z.lo = v>>(64-n), v<<n
	}
	return z
}
func (z *big128) add(a, b *big128) *big128 {
	lo := a.lo + b.lo
	carry := uint64(0)
	if lo < a.lo {
		carry = 1
	}
	z.hi, z.lo = a.hi+b.hi+carry, lo
	return z
}
func (z *big128) cmp(o *big128) int {
	if z.hi != o.hi {
		if z.hi < o.hi {
			return -1
		}
		return 1
	}
	if z.lo != o.lo {
		if z.lo < o.lo {
			return -1
		}
		return 1
	}
	return 0
}
