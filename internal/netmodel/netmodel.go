// Package netmodel defines the network model shared by the routing backend:
// address families, canonical prefixes, routes and next hops.
//
// Two invariants are enforced here and relied on everywhere else:
//
//  1. Prefix normalization: every Prefix stored in the model is masked to its
//     network address (host bits cleared) and renders in canonical form
//     (RFC 5952 for IPv6). Parsing is strict: IPv4 octets with leading zeros
//     are rejected, IPv4-mapped IPv6 forms are rejected.
//  2. Address-family isolation: every Prefix, Addr and Route carries an
//     explicit Family. The RIB keeps one compressed prefix tree per family
//     and never lets a lookup or a recursive next-hop resolution cross
//     families.
package netmodel

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"strings"
)

// Family identifies an address family. Tables are isolated per family.
type Family int

const (
	FamilyV4 Family = 4
	FamilyV6 Family = 6
)

func (f Family) String() string {
	switch f {
	case FamilyV4:
		return "ipv4"
	case FamilyV6:
		return "ipv6"
	default:
		return fmt.Sprintf("family(%d)", int(f))
	}
}

// ParseFamily accepts "ipv4"/"ipv6" (also "4"/"6", "v4"/"v6").
func ParseFamily(s string) (Family, error) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "ipv4", "4", "v4", "inet":
		return FamilyV4, nil
	case "ipv6", "6", "v6", "inet6":
		return FamilyV6, nil
	default:
		return 0, fmt.Errorf("netmodel: unknown address family %q", s)
	}
}

// Addr is a normalized IP address with an explicit family.
type Addr struct {
	ip netip.Addr
}

// ParseAddr parses a textual IP address. IPv4-mapped IPv6 addresses are
// rejected: they would blur family isolation.
func ParseAddr(s string) (Addr, error) {
	ip, err := netip.ParseAddr(strings.TrimSpace(s))
	if err != nil {
		return Addr{}, fmt.Errorf("netmodel: invalid address %q: %w", s, err)
	}
	if ip.Is4In6() {
		return Addr{}, fmt.Errorf("netmodel: IPv4-mapped IPv6 address %q is not accepted", s)
	}
	return Addr{ip: ip}, nil
}

func addrFromIP(ip netip.Addr) Addr { return Addr{ip: ip} }

// Family of the address.
func (a Addr) Family() Family {
	if a.ip.Is4() {
		return FamilyV4
	}
	return FamilyV6
}

// IsZero reports whether the address is unset.
func (a Addr) IsZero() bool { return !a.ip.IsValid() }

func (a Addr) String() string { return a.ip.String() }

// Bytes returns the address in its family's native width: 4 bytes for IPv4,
// 16 for IPv6. It is the key encoding consumed by the compressed prefix tree.
func (a Addr) Bytes() []byte { return a.ip.AsSlice() }

// Prefix is a normalized network prefix: host bits are always cleared and the
// family is fixed by construction.
type Prefix struct {
	pfx netip.Prefix // always Masked()
}

// ParsePrefix parses "addr/len" and normalizes host bits away. The textual
// form must not use IPv4-mapped IPv6 notation; IPv6 zones are not allowed.
func ParsePrefix(s string) (Prefix, error) {
	p, err := netip.ParsePrefix(strings.TrimSpace(s))
	if err != nil {
		return Prefix{}, fmt.Errorf("netmodel: invalid prefix %q: %w", s, err)
	}
	if p.Addr().Is4In6() {
		return Prefix{}, fmt.Errorf("netmodel: IPv4-mapped IPv6 prefix %q is not accepted", s)
	}
	return Prefix{pfx: p.Masked()}, nil
}

// MustPrefix is ParsePrefix that panics on error; for tests and fixtures.
func MustPrefix(s string) Prefix {
	p, err := ParsePrefix(s)
	if err != nil {
		panic(err)
	}
	return p
}

func prefixFromNetip(p netip.Prefix) Prefix { return Prefix{pfx: p.Masked()} }

// PrefixFrom builds a normalized prefix from an address and a length.
func PrefixFrom(a Addr, bits int) Prefix {
	return Prefix{pfx: netip.PrefixFrom(a.ip, bits).Masked()}
}

// Family of the prefix.
func (p Prefix) Family() Family {
	if p.pfx.Addr().Is4() {
		return FamilyV4
	}
	return FamilyV6
}

// Bits returns the prefix length.
func (p Prefix) Bits() int { return p.pfx.Bits() }

// Addr returns the (normalized) network address.
func (p Prefix) Addr() Addr { return addrFromIP(p.pfx.Addr()) }

// IsZero reports whether the prefix is unset.
func (p Prefix) IsZero() bool { return !p.pfx.Addr().IsValid() }

// String renders the canonical form, e.g. "2001:db8::/32".
func (p Prefix) String() string { return p.pfx.String() }

// Contains reports whether a falls inside p. Cross-family checks are false.
func (p Prefix) Contains(a Addr) bool {
	if a.IsZero() || p.Family() != a.Family() {
		return false
	}
	return p.pfx.Contains(a.ip)
}

// MarshalJSON renders the canonical string; an unset prefix renders as ""
// (used inside delete-change envelopes).
func (p Prefix) MarshalJSON() ([]byte, error) {
	if !p.pfx.Addr().IsValid() {
		return []byte(`""`), nil
	}
	return json.Marshal(p.String())
}

// UnmarshalJSON parses and normalizes; "" yields the zero value.
func (p *Prefix) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	if s == "" {
		*p = Prefix{}
		return nil
	}
	pfx, err := ParsePrefix(s)
	if err != nil {
		return err
	}
	*p = pfx
	return nil
}

// MarshalJSON renders the canonical address string; an unset address renders
// as "" so direct (interface-only) next hops round-trip cleanly.
func (a Addr) MarshalJSON() ([]byte, error) {
	if !a.ip.IsValid() {
		return []byte(`""`), nil
	}
	return json.Marshal(a.String())
}

// UnmarshalJSON parses an address; "" yields the zero value.
func (a *Addr) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	if s == "" {
		*a = Addr{}
		return nil
	}
	ad, err := ParseAddr(s)
	if err != nil {
		return err
	}
	*a = ad
	return nil
}

// NextHop describes how traffic for a route leaves the router. Exactly one of
// the two forms is used:
//   - direct: an egress interface (connected route), or
//   - recursive: an IP address that must itself be resolved through the same
//     address family's table.
type NextHop struct {
	Addr      Addr   `json:"addr,omitempty"`
	Interface string `json:"interface,omitempty"`
}

// IsDirect reports whether the next hop is an attached interface.
func (n NextHop) IsDirect() bool { return n.Addr.IsZero() && n.Interface != "" }

// IsRecursive reports whether the next hop is an IP needing resolution.
func (n NextHop) IsRecursive() bool { return !n.Addr.IsZero() }

// Validate enforces the exactly-one-form rule.
func (n NextHop) Validate() error {
	switch {
	case n.IsDirect():
		return nil
	case n.IsRecursive():
		if n.Interface != "" {
			return fmt.Errorf("netmodel: recursive next hop %s must not also name an interface", n.Addr)
		}
		return nil
	default:
		return fmt.Errorf("netmodel: next hop needs either an interface or an address")
	}
}

// Route is a single RIB candidate for a prefix.
type Route struct {
	ID        string  `json:"id"`
	Prefix    Prefix  `json:"prefix"`
	AdminDist int     `json:"admin_distance"`
	Metric    uint32  `json:"metric"`
	NextHop   NextHop `json:"next_hop"`
	// Meta carries free-form operator attributes. Values under secret-looking
	// keys (token, password, secret, key, community) are redacted in logs.
	Meta map[string]string `json:"meta,omitempty"`

	// Seq is a monotonically increasing sequence assigned by the RIB at
	// upsert time; it is the final deterministic tie-break. InstalledVersion
	// records the table version that installed the route (batch visibility).
	Seq              uint64 `json:"seq,omitempty"`
	InstalledVersion uint64 `json:"installed_version,omitempty"`
}

// Family of the route, derived from its prefix.
func (r Route) Family() Family { return r.Prefix.Family() }

// Validate checks structural correctness of a route. Semantic checks that
// need table context (e.g. next-hop family vs. prefix family) live in rib.
func (r Route) Validate() error {
	if r.ID == "" {
		return fmt.Errorf("netmodel: route id must not be empty")
	}
	if r.Prefix.IsZero() {
		return fmt.Errorf("netmodel: route %q has no prefix", r.ID)
	}
	if r.AdminDist < 0 || r.AdminDist > 255 {
		return fmt.Errorf("netmodel: route %q admin_distance %d out of range [0,255]", r.ID, r.AdminDist)
	}
	if err := r.NextHop.Validate(); err != nil {
		return fmt.Errorf("netmodel: route %q: %w", r.ID, err)
	}
	if r.NextHop.IsRecursive() && r.NextHop.Addr.Family() != r.Prefix.Family() {
		return fmt.Errorf("netmodel: route %q next hop %s is %s but prefix %s is %s (cross-family resolution is not supported)",
			r.ID, r.NextHop.Addr, r.NextHop.Addr.Family(), r.Prefix, r.Prefix.Family())
	}
	return nil
}
