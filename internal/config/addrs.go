package config

import (
	"fmt"
	"net/netip"
)

func parseV4(s string) (*netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return nil, err
	}
	a = a.Unmap()
	if !a.Is4() {
		return nil, fmt.Errorf("%q is not an IPv4 address", s)
	}
	return &a, nil
}

func ipToUint(a netip.Addr) uint32 {
	b := a.As4()
	return uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
}

func isLoopbackV4(a netip.Addr) bool {
	b := a.As4()
	return b[0] == 127
}
