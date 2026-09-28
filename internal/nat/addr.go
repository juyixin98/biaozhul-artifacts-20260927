package nat

import "net/netip"

// parseAddr is the single address parser used by validation and topology
// checks.
func parseAddr(s string) (netip.Addr, error) {
	return netip.ParseAddr(s)
}
