package model

import (
	"fmt"
	"net/netip"
	"strconv"
	"strings"

	"pathvector/internal/ierr"
)

// Origin is the BGP ORIGIN attribute; lower value is preferred.
type Origin uint8

const (
	OriginIGP        Origin = 0
	OriginEGP        Origin = 1
	OriginIncomplete Origin = 2
)

// ParseOrigin accepts the canonical spelling used in fixtures.
func ParseOrigin(s string) (Origin, error) {
	switch strings.ToLower(s) {
	case "", "igp":
		return OriginIGP, nil
	case "egp":
		return OriginEGP, nil
	case "incomplete":
		return OriginIncomplete, nil
	}
	return 0, ierr.New(ierr.KindInvalidInput, "model.ParseOrigin", "unknown origin "+s)
}

// String renders the canonical spelling.
func (o Origin) String() string {
	switch o {
	case OriginIGP:
		return "igp"
	case OriginEGP:
		return "egp"
	case OriginIncomplete:
		return "incomplete"
	}
	return "invalid(" + strconv.Itoa(int(o)) + ")"
}

// Attrs are the supported path-vector routing attributes. The supported
// comparison order is implemented in package engine/compare.go:
//
//	local-pref (higher) > AS path length (shorter) > origin (lower) >
//	MED (lower, comparable AS pairs only) > eBGP over iBGP > IGP cost
//	(lower) > neighbor router id (lower, final deterministic tie-break).
type Attrs struct {
	LocalPref uint32 `json:"local_pref"`
	ASPath    []int  `json:"as_path"`
	MED       uint32 `json:"med"`
	Origin    Origin `json:"origin"`
	// LearnedIBGP records whether the route was received over an iBGP
	// session. It is an internal-only attribute: import policies cannot
	// set it; the engine assigns it from the session type.
	LearnedIBGP bool `json:"learned_ibgp"`
}

// PathLen is the number of AS_SEQUENCE segments currently supported (one).
func (a Attrs) PathLen() int { return len(a.ASPath) }

// ContainsAS reports whether the AS_PATH already carries asn (loop guard).
func (a Attrs) ContainsAS(asn int) bool {
	for _, x := range a.ASPath {
		if x == asn {
			return true
		}
	}
	return false
}

// Candidate is one entry in a router's Adj-RIB-In: a route for Prefix
// learned from a specific neighbor router (or locally originated).
type Candidate struct {
	Prefix   string `json:"prefix"`
	FromPeer string `json:"from_peer"` // empty for local origin
	NextHop  string `json:"next_hop"`
	Attrs    Attrs  `json:"attrs"`
}

// LocalOrigin marks a locally injected seed route.
const LocalOrigin = ""

// ParsePrefix parses and canonicalizes an IPv4/IPv6 prefix, requiring the
// masked form (10.1.0.0/16, not 10.1.2.3/16).
func ParsePrefix(s string) (string, error) {
	p, err := netip.ParsePrefix(s)
	if err != nil {
		return "", ierr.Wrap(ierr.KindInvalidInput, "model.ParsePrefix", "bad prefix "+s, err)
	}
	p = p.Masked()
	if s != p.String() {
		return "", ierr.New(ierr.KindInvalidInput, "model.ParsePrefix",
			fmt.Sprintf("prefix %s not in masked canonical form (use %s)", s, p.String()))
	}
	return p.String(), nil
}

// ParseAddress parses a single IP literal for next_hop.
func ParseAddress(s string) error {
	if _, err := netip.ParseAddr(s); err != nil {
		return ierr.Wrap(ierr.KindInvalidInput, "model.ParseAddress", "bad next_hop "+s, err)
	}
	return nil
}
