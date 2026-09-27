// Package redact masks sensitive values (member IP addresses) before they
// are written to logs. Stored data and API responses keep full values —
// this is a local, loopback tool — but log lines must never leak full
// member addresses.
package redact

import (
	"fmt"
	"net/netip"
)

// IP masks an IP address for logging: the last octet of an IPv4 address
// becomes "x"; an IPv6 address is truncated to its first four hextets.
// A value that does not parse as an IP is returned unchanged (it was
// never sensitive to begin with).
func IP(s string) string {
	addr, err := netip.ParseAddr(s)
	if err != nil {
		return s
	}
	if addr.Is4() {
		b := addr.As4()
		return fmt.Sprintf("%d.%d.%d.x", b[0], b[1], b[2])
	}
	b := addr.As16()
	prefix := netip.AddrFrom16([16]byte{b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]})
	return prefix.String() + "..."
}
