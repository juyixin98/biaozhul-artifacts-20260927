// Package netmodel contains the core address-set model and the minimal
// non-overlapping CIDR cover algorithm.
//
// All arithmetic is performed on big.Int intervals of address ordinals
// (0 .. 2^bits-1); addresses themselves are never enumerated. The bit width
// is parameterised (32 for IPv4, 128 for IPv6), and the same implementation
// serves both families and the smaller widths used by the exhaustive tests.
package netmodel

import (
	"fmt"
	"math/big"
	"net/netip"
	"strings"
)

// Bits of the address family.
const (
	IPv4Bits = 32
	IPv6Bits = 128
)

// Prefix is a canonical CIDR block: a network address together with a prefix
// length. Prefix is stored as the zero-host address so that values compare
// deterministically and never carry stray host bits.
type Prefix struct {
	addr   netip.Addr
	prefix uint8
}

// ParsePrefix parses "10.0.0.0/8" or "2001:db8::/32".
//
// Errors carry a typed Kind (see PrefixError) so callers can assert on the
// failure category. By default host bits are rejected (strict); callers that
// prefer canonicalisation may use ParsePrefixLenient.
func ParsePrefix(s string) (Prefix, error) {
	return parsePrefix(s, false)
}

// ParsePrefixLenient parses a CIDR and masks off any host bits instead of
// rejecting them; a warning describing the canonicalisation is returned.
func ParsePrefixLenient(s string) (Prefix, string, error) {
	p, err := parsePrefix(s, true)
	if err != nil {
		return Prefix{}, "", err
	}
	canon := p.String()
	warn := ""
	if canon != strings.TrimSpace(s) {
		warn = fmt.Sprintf("input %q contains host bits; canonicalized to %q", s, canon)
	}
	return p, warn, nil
}

func parsePrefix(s string, lenient bool) (Prefix, error) {
	s = strings.TrimSpace(s)
	ip, err := netip.ParsePrefix(s)
	if err != nil {
		return Prefix{}, classifyNetipError(s, err)
	}
	bits := ip.Addr().BitLen()
	if ip.Bits() > bits {
		return Prefix{}, &PrefixError{
			Kind:  KindPrefixTooLong,
			Input: s,
			msg:   fmt.Sprintf("prefix length %d exceeds address width %d", ip.Bits(), bits),
		}
	}
	if !lenient && ip.Addr() != ip.Masked().Addr() {
		return Prefix{}, &PrefixError{
			Kind:  KindHostBits,
			Input: s,
			msg:   fmt.Sprintf("address has non-zero host bits; canonical form is %s", ip.Masked()),
		}
	}
	p := Prefix{addr: ip.Masked().Addr(), prefix: uint8(ip.Bits())}
	return p, nil
}

// classifyNetipError maps the (unexported) net/netip parse errors onto our
// typed failure kinds by inspecting their stable message text.
func classifyNetipError(s string, err error) error {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "prefix length out of range"):
		return &PrefixError{Kind: KindPrefixTooLong, Input: s, msg: msg}
	default:
		// "no '/'", "bad bits after slash", bad address text, IPv6 zones, ...
		return &PrefixError{Kind: KindMalformed, Input: s, msg: msg}
	}
}

// Addr returns the (zero-host) network address.
func (p Prefix) Addr() netip.Addr { return p.addr }

// Bits returns the prefix length.
func (p Prefix) Bits() int { return int(p.prefix) }

// FamilyBits returns 32 or 128.
func (p Prefix) FamilyBits() int { return p.addr.BitLen() }

// String renders the canonical CIDR.
func (p Prefix) String() string {
	if !p.addr.IsValid() {
		return "<invalid>"
	}
	return fmt.Sprintf("%s/%d", p.addr, p.prefix)
}

// Interval returns the half-open ordinal interval [start, end) covered by the
// prefix, where address a is interpreted as a big-endian unsigned integer.
// The interval is inclusive of the network address and of the directed
// broadcast address; that boundary policy is fixed and documented.
func (p Prefix) Interval() (start, end *big.Int) {
	return PrefixInterval(p.addr.BitLen(), p.rawBytes(), int(p.prefix))
}

func (p Prefix) rawBytes() []byte {
	if p.addr.Is4() {
		b := p.addr.As4()
		return b[:]
	}
	b := p.addr.As16()
	return b[:]
}

// Error kinds surfaced by ParsePrefix.
const (
	KindMalformed      = "malformed_cidr"
	KindPrefixTooLong  = "prefix_length_too_long"
	KindHostBits       = "host_bits_present"
	KindFamilyMismatch = "family_mismatch"
)

// PrefixError is the typed parse failure.
type PrefixError struct {
	Kind  string
	Input string
	msg   string
}

func (e *PrefixError) Error() string {
	return fmt.Sprintf("%s: %q: %s", e.Kind, e.Input, e.msg)
}

// PrefixInterval computes [start,end) for a prefix of the given width whose
// network bytes are the leading bytes of raw (big-endian). Exported for the
// width-parameterised construction used by tests.
func PrefixInterval(width int, raw []byte, prefixLen int) (start, end *big.Int) {
	start = new(big.Int).SetBytes(normalizeWidth(width, raw))
	size := new(big.Int).Lsh(big.NewInt(1), uint(width-prefixLen))
	end = new(big.Int).Add(start, size)
	return start, end
}

// normalizeWidth re-slices big-endian raw bytes to width/8 bytes (rounding up
// for non-byte widths used by the exhaustive tests), right-aligned.
func normalizeWidth(width int, raw []byte) []byte {
	need := width / 8
	if width%8 != 0 {
		need++
	}
	out := make([]byte, need)
	if len(raw) >= need {
		copy(out, raw[len(raw)-need:])
	} else {
		copy(out[need-len(raw):], raw)
	}
	return out
}

// MustParsePrefix panics on error; for tests and fixtures only.
func MustParsePrefix(s string) Prefix {
	p, err := ParsePrefix(s)
	if err != nil {
		panic(err)
	}
	return p
}
