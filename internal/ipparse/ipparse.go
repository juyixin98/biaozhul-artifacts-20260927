// Package ipparse turns user-supplied CIDR or bare-IP strings into fixed-width
// big-integer intervals and renders prefixes back into canonical text.
//
// Parsing uses net/netip from the standard library; this package adds:
//   - explicit, typed failure classes (never a bare "parse error"),
//   - detection of non-canonical input (host bits set / mapped IPv4-in-IPv6),
//   - conversion into the width-independent interval model of netmodel.
package ipparse

import (
	"fmt"
	"math/big"
	"net/netip"
	"strconv"
	"strings"

	"cidrcov/internal/netmodel"
)

// Failure classes produced by Parse. These strings are stable API values.
const (
	ClassInvalidSyntax = "invalid_syntax" // not a CIDR/IP netip can parse
	ClassBadPrefixLen  = "bad_prefix_len" // /-1, /33 on v4, /129 on v6, mapped v6 shorter than /96 ...
)

// ParseError carries a machine-readable class plus the offending input.
type ParseError struct {
	Class  string
	Input  string
	Detail string
}

func (e *ParseError) Error() string {
	return fmt.Sprintf("%s: %q (%s)", e.Class, e.Input, e.Detail)
}

// Address family identifiers.
const (
	KindV4 = "ipv4" // width 32
	KindV6 = "ipv6" // width 128
)

// ParsedEntry is one normalized allow/exclude list element.
type ParsedEntry struct {
	// Raw is exactly what the caller submitted.
	Raw string
	// Kind is KindV4 or KindV6.
	Kind string
	// PrefixLen is in the native width (32- or 128-based).
	PrefixLen int
	// Network is the aligned network address as a big integer of the
	// native family width (host bits zeroed).
	Network *big.Int
	// Interval covers the whole prefix INCLUDING network and broadcast.
	Interval netmodel.Interval
	// CanonicalText is the re-rendered canonical form, e.g. 10.0.0.0/24.
	CanonicalText string
}

// Advisories emitted during parsing. Advisories are NOT failures: the entry
// is still accepted, but the caller's mental model likely needs correcting.
const (
	AdvHostBitsCanonicalized = "host_bits_canonicalized"
	AdvBareIPExpanded        = "bare_ip_expanded_to_host_prefix"
	AdvV4MappedInV6          = "ipv4_mapped_in_ipv6_rebased"
)

// Advisory records one non-fatal normalization with enough detail to explain
// the difference between submitted and effective input.
type Advisory struct {
	Code      string `json:"code"`
	Input     string `json:"input"`
	Message   string `json:"message"`
	Effective string `json:"effective"`
}

// Parse accepts "a.b.c.d/len", "a.b.c.d" (=> /32), "v6/len" and bare v6.
// IPv4-mapped IPv6 input (::ffff:1.2.3.4/...) is rebased onto the 32-bit
// IPv4 space and reported via an Advisory; v4 and v6 are never mixed silently.
func Parse(s string) (ParsedEntry, []Advisory, error) {
	var advs []Advisory

	// Pre-classify an explicit prefix length that is merely out of range so
	// netip's combined "unexpected character" parse error is not reported as a
	// syntax failure. "10.0.0.0/33" is a length problem, not a malformed token.
	if i := strings.LastIndexByte(s, '/'); i >= 0 {
		addrText, lenText := s[:i], s[i+1:]
		n, lerr := strconv.Atoi(lenText)
		addr, aerr := netip.ParseAddr(addrText)
		if lerr == nil && aerr == nil {
			// Validate against the NOTATION width: a v4-mapped v6 literal is
			// written with a 0..128 length (e.g. ::ffff:10.0.0.0/120); the
			// stricter mapped-subset check (< /96) happens after rebase below.
			notationWidth := 128
			if addr.Is4() {
				notationWidth = 32
			}
			if n < 0 || n > notationWidth {
				return ParsedEntry{}, nil, &ParseError{Class: ClassBadPrefixLen, Input: s,
					Detail: fmt.Sprintf("prefix length %d outside 0..%d for %s", n, notationWidth, addrText)}
			}
		}
	}

	pref, perr := netip.ParsePrefix(s)
	bare := false
	if perr != nil {
		// Try a bare address: expand to host prefix (/32 or /128).
		addr, aerr := netip.ParseAddr(s)
		if aerr != nil {
			return ParsedEntry{}, nil, &ParseError{Class: ClassInvalidSyntax, Input: s, Detail: aerr.Error()}
		}
		pref = netip.PrefixFrom(addr, addr.BitLen())
		bare = true
	}

	if pref.Bits() < 0 || pref.Bits() > pref.Addr().BitLen() {
		return ParsedEntry{}, nil, &ParseError{Class: ClassBadPrefixLen, Input: s,
			Detail: fmt.Sprintf("prefix length %d outside 0..%d", pref.Bits(), pref.Addr().BitLen())}
	}

	rawAddr := pref.Addr()
	kind := KindV6
	width := 128
	wasMapped := rawAddr.Is4In6()
	if rawAddr.Is4() || wasMapped {
		kind = KindV4
		width = 32
	}

	nativeLen := pref.Bits()
	// Unmap: 4in6 becomes a plain v4 address and the prefix length rebases
	// from the v6 scale onto the v4 scale.
	addr := rawAddr.Unmap()
	if wasMapped {
		// A v4-mapped v6 prefix /L (L>=96) covers the same low v4 bits as
		// a native v4 prefix of length L-96. Shorter prefixes cannot be
		// expressed in the mapped 32 low bits and are rejected.
		if nativeLen < 96 {
			return ParsedEntry{}, nil, &ParseError{Class: ClassBadPrefixLen, Input: s,
				Detail: fmt.Sprintf("ipv4-mapped ipv6 prefix /%d spans beyond the mapped 32 low bits", nativeLen)}
		}
		nativeLen -= 96
	}

	// Detect host bits: compare the unmapped address against its native mask.
	nativePref, err := netip.ParsePrefix(fmt.Sprintf("%s/%d", addr.String(), nativeLen))
	if err != nil {
		return ParsedEntry{}, nil, &ParseError{Class: ClassBadPrefixLen, Input: s, Detail: err.Error()}
	}
	hostBits := addr != nativePref.Masked().Addr()

	effective := fmt.Sprintf("%s/%d", nativePref.Masked().Addr().String(), nativeLen)
	if bare {
		advs = append(advs, Advisory{Code: AdvBareIPExpanded, Input: s,
			Message:   fmt.Sprintf("bare address expanded to /%d", width),
			Effective: effective})
	}
	if wasMapped {
		advs = append(advs, Advisory{Code: AdvV4MappedInV6, Input: s,
			Message:   "ipv4-mapped ipv6 notation rebased onto native ipv4 space",
			Effective: effective})
	}
	if hostBits && !bare {
		advs = append(advs, Advisory{Code: AdvHostBitsCanonicalized, Input: s,
			Message:   "host bits set; masked to network boundary (a prefix always includes network and broadcast addresses)",
			Effective: effective})
	}

	maskedNet := nativePref.Masked().Addr()
	net, last, ok := netmodel.PrefixToInterval(width, nativeLen, addrToBig(maskedNet))
	if !ok {
		return ParsedEntry{}, nil, &ParseError{Class: ClassBadPrefixLen, Input: s, Detail: "prefix does not fit address width"}
	}
	return ParsedEntry{
		Raw:           s,
		Kind:          kind,
		PrefixLen:     nativeLen,
		Network:       new(big.Int).Set(net),
		Interval:      netmodel.Interval{Start: net, End: last},
		CanonicalText: effective,
	}, advs, nil
}

func addrToBig(a netip.Addr) *big.Int {
	// AsSlice yields 4 bytes for v4 and 16 for v6; never As16, whose
	// zero-padding of a 4-byte slice would read every v4 address as 0.
	return new(big.Int).SetBytes(a.AsSlice())
}

func bigToAddr(width int, n *big.Int) netip.Addr {
	nb := n.Bytes()
	if width == 32 {
		var b [4]byte
		copy(b[4-len(nb):], nb)
		return netip.AddrFrom4(b)
	}
	var b [16]byte
	copy(b[16-len(nb):], nb)
	return netip.AddrFrom16(b)
}

// Format renders a native-family prefix to canonical text.
func Format(kind string, p netmodel.Prefix) string {
	width := 128
	if kind == KindV4 {
		width = 32
	}
	return fmt.Sprintf("%s/%d", bigToAddr(width, p.Base), p.Len)
}
