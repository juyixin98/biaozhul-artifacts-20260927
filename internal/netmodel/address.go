package netmodel

import (
	"fmt"
	"math/big"
	"net/netip"
)

// AddrSet is a 1-D set over one address family.
type AddrSet struct {
	Family Family
	Vals   Int1D
}

// AddressBits returns the domain width for a family.
func AddressBits(f Family) int {
	if f == FamilyV6 {
		return BitsIPv6
	}
	return BitsIPv4
}

// CIDRInfo describes a parsed CIDR.
type CIDRInfo struct {
	Text        string
	Family      Family
	Prefix      netip.Prefix
	HostBitsSet bool // true when host bits were present (they are masked off)
	Lo, Hi      *big.Int
}

// ParseCIDR parses an "ip/prefixlen" literal. Unlike net.ParseCIDR it reports
// whether the textual address had host bits set (a soft error: matching uses
// the network address, but the rule text is almost certainly a mistake).
func ParseCIDR(text string) (CIDRInfo, error) {
	pfx, err := netip.ParsePrefix(text)
	if err != nil {
		// Tolerate bare addresses: treat as host prefix.
		addr, aerr := netip.ParseAddr(text)
		if aerr != nil {
			return CIDRInfo{}, fmt.Errorf("invalid CIDR %q: %v", text, err)
		}
		pfx = netip.PrefixFrom(addr, addr.BitLen())
	}
	pfx = pfx.Masked()
	// Re-derive host-bits: compare canonical masked text to input.
	orig, err := netip.ParsePrefix(text)
	hostBits := false
	if err == nil && orig.Addr().Compare(pfx.Addr()) != 0 {
		hostBits = true
	}
	fam := FamilyV4
	bits := BitsIPv4
	if pfx.Addr().Is6() {
		fam = FamilyV6
		bits = BitsIPv6
	}
	lo := addrToInt(pfx.Addr())
	size := pfx.Bits()
	hostCount := new(big.Int).Lsh(big.NewInt(1), uint(bits-size))
	hi := new(big.Int).Add(lo, new(big.Int).Sub(hostCount, big.NewInt(1)))
	return CIDRInfo{Text: text, Family: fam, Prefix: pfx, HostBitsSet: hostBits, Lo: lo, Hi: hi}, nil
}

// AddrSetFromCIDRs unions the given CIDRs; all must belong to one family.
func AddrSetFromCIDRs(cidrs []CIDRInfo) (AddrSet, error) {
	if len(cidrs) == 0 {
		return AddrSet{}, fmt.Errorf("empty address set")
	}
	fam := cidrs[0].Family
	var segs []Seg
	for _, c := range cidrs {
		if c.Family != fam {
			return AddrSet{}, fmt.Errorf("mixed address families in rule: %s vs %s", fam, c.Family)
		}
		segs = append(segs, Seg{new(big.Int).Set(c.Lo), new(big.Int).Set(c.Hi)})
	}
	s, err := FromSegments(AddressBits(fam), segs)
	if err != nil {
		return AddrSet{}, err
	}
	return AddrSet{Family: fam, Vals: s}, nil
}

// UniverseAddrSet returns 0.0.0.0/0 or ::/0 for the family.
func UniverseAddrSet(fam Family) AddrSet {
	return AddrSet{Family: fam, Vals: Universe1D(AddressBits(fam))}
}

func addrToInt(a netip.Addr) *big.Int {
	b := a.As16() // 4in6 representation for v4? As16 on v4 returns 16-byte with zeros
	if a.Is4() {
		b4 := a.As4()
		return new(big.Int).SetBytes(b4[:])
	}
	return new(big.Int).SetBytes(b[:])
}

// IntToAddr renders an integer in the family domain as an IP address.
func IntToAddr(fam Family, v *big.Int) netip.Addr {
	b := v.Bytes()
	width := 4
	if fam == FamilyV6 {
		width = 16
	}
	buf := make([]byte, width)
	copy(buf[width-len(b):], b)
	if fam == FamilyV6 {
		var arr [16]byte
		copy(arr[:], buf)
		return netip.AddrFrom16(arr).Unmap()
	}
	var arr [4]byte
	copy(arr[:], buf)
	return netip.AddrFrom4(arr)
}
