package analyzercheck_test

import (
	"net/netip"
)

func parseIPv4(s string) (uint32, netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return 0, netip.Addr{}, err
	}
	b := a.As4()
	v := uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
	return v, a, nil
}
