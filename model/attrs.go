package model

import (
	"fmt"
	"strconv"
	"strings"
)

// Prefix is a reachable network identifier. The backend is address-family
// agnostic: values are opaque non-empty labels (e.g. "203.0.113.0/24" or
// "fd00::/32") that are compared for equality only.
type Prefix string

// Attrs holds the supported path-vector routing attributes.
//
// Fields intentionally limited to what the comparison order in Compare
// actually uses; unsupported attributes are rejected at config parse time
// (see README "Support scope").
type Attrs struct {
	// LocalPref: higher is preferred. Meaningful on iBGP-learned/internal
	// choice; eBGP inbound events must not carry it (it is an iBGP-only
	// transitive-within-AS attribute).
	LocalPref *int `json:"local_pref,omitempty"`
	// ASPath: ordered AS numbers, most recently prepended AS first
	// (BGP convention). Empty means locally originated.
	ASPath []uint32 `json:"as_path"`
	// MED: lower is preferred, compared only when the first (neighboring)
	// AS of both candidates is equal.
	Med *int `json:"med,omitempty"`
	// Origin: IGP < EGP < incomplete.
	Origin Origin `json:"origin"`
}

// LocalPrefOr returns the value, or the default (100) when unset.
func (a Attrs) LocalPrefOr(def int) int {
	if a.LocalPref != nil {
		return *a.LocalPref
	}
	return def
}

// MedOr returns the value or 0 when unset.
func (a Attrs) MedOr() int {
	if a.Med != nil {
		return *a.Med
	}
	return 0
}

// FirstAS returns the leftmost AS in the path and whether the path is
// non-empty. Used for the "MED only between same neighbor AS" rule.
func (a Attrs) FirstAS() (uint32, bool) {
	if len(a.ASPath) == 0 {
		return 0, false
	}
	return a.ASPath[0], true
}

// ContainsAS reports whether the AS path already carries asn.
func (a Attrs) ContainsAS(asn uint32) bool {
	for _, x := range a.ASPath {
		if x == asn {
			return true
		}
	}
	return false
}

// Clone returns a deep copy (slices are copied so candidates never alias
// event/policy scratch arrays).
func (a Attrs) Clone() Attrs {
	c := a
	if a.ASPath != nil {
		c.ASPath = append([]uint32(nil), a.ASPath...)
	}
	if a.LocalPref != nil {
		v := *a.LocalPref
		c.LocalPref = &v
	}
	if a.Med != nil {
		v := *a.Med
		c.Med = &v
	}
	return c
}

// ClonePtr returns a pointer to a deep-copied Attrs.
func (a Attrs) ClonePtr() *Attrs {
	c := a.Clone()
	return &c
}

// Reason identifies the tie-break that decided a route comparison.
type Reason string

const (
	ReasonLocalPref    Reason = "local_pref"
	ReasonASPath       Reason = "as_path_length"
	ReasonMED          Reason = "med"
	ReasonOrigin       Reason = "origin"
	ReasonEbgpOverIbgp Reason = "ebgp_over_ibgp"
	ReasonIGPCost      Reason = "igp_cost_to_next_hop"
	ReasonRouterID     Reason = "router_id"
)

// CompareInput supplies the topology-side facts the comparison order needs
// beyond the attributes themselves.
type CompareInput struct {
	// SessionType of the candidate: "ebgp" or "ibgp".
	SessionType string
	// IGPCost is the cost from the choosing router to the route's next-hop
	// router (0 for eBGP-adjacent and self-originated).
	IGPCost int
	// PeerOrdinal is the peer router's declaration order, used as the
	// final deterministic router-id tie-break.
	PeerOrdinal int
	// ReceivedVersion is the event delivery version at which this
	// candidate was last installed — the "oldest learned" tie-break.
	ReceivedVersion int
}

// Compare implements the supported best-path selection order.
//
// Order (highest precedence first), deliberately explicit:
//
//  1. LocalPref (higher)
//  2. AS_PATH length (shorter)
//  3. MED (lower), only when first AS of both paths is equal
//  4. ORIGIN (IGP < EGP < incomplete)
//  5. eBGP-learned over iBGP-learned
//  6. IGP cost to next-hop (lower)
//  7. peer router id (lower declaration ordinal)
//  8. oldest learned (lower received version)
//
// Returns -1 when a is strictly better, +1 when b is strictly better,
// and the reason of the deciding step. It never returns 0: the last two
// steps guarantee a total, deterministic order among distinct candidates.
func Compare(a, b Attrs, ai, bi CompareInput) (int, Reason) {
	// 1. LocalPref.
	if va, vb := a.LocalPrefOr(100), b.LocalPrefOr(100); va != vb {
		if va > vb {
			return -1, ReasonLocalPref
		}
		return 1, ReasonLocalPref
	}
	// 2. AS_PATH length.
	if la, lb := len(a.ASPath), len(b.ASPath); la != lb {
		if la < lb {
			return -1, ReasonASPath
		}
		return 1, ReasonASPath
	}
	// 3. MED, only between routes of the same neighboring AS.
	if fa, oka := a.FirstAS(); oka {
		if fb, okb := b.FirstAS(); okb && fa == fb {
			if ma, mb := a.MedOr(), b.MedOr(); ma != mb {
				if ma < mb {
					return -1, ReasonMED
				}
				return 1, ReasonMED
			}
		}
	}
	// 4. Origin.
	if a.Origin != b.Origin {
		if a.Origin < b.Origin {
			return -1, ReasonOrigin
		}
		return 1, ReasonOrigin
	}
	// 5. eBGP over iBGP.
	if ai.SessionType != bi.SessionType {
		if ai.SessionType == "ebgp" {
			return -1, ReasonEbgpOverIbgp
		}
		return 1, ReasonEbgpOverIbgp
	}
	// 6. IGP cost to next-hop.
	if ai.IGPCost != bi.IGPCost {
		if ai.IGPCost < bi.IGPCost {
			return -1, ReasonIGPCost
		}
		return 1, ReasonIGPCost
	}
	// 7. Router id (stable declaration ordinal).
	if ai.PeerOrdinal != bi.PeerOrdinal {
		if ai.PeerOrdinal < bi.PeerOrdinal {
			return -1, ReasonRouterID
		}
		return 1, ReasonRouterID
	}
	// 8. Oldest learned (two distinct candidates of one router cannot
	// normally share a peer, but keep the order total regardless).
	if ai.ReceivedVersion != bi.ReceivedVersion {
		if ai.ReceivedVersion < bi.ReceivedVersion {
			return -1, ReasonRouterID
		}
		return 1, ReasonRouterID
	}
	// Fully tied — candidates are indistinguishable; treat as equal.
	return 0, ReasonRouterID
}

// ParseASPath parses a textual AS path such as "65003 65001" (space or
// comma separated); used by CLI/fixture helpers, not the JSON path.
func ParseASPath(s string) ([]uint32, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil, nil
	}
	fields := strings.FieldsFunc(s, func(r rune) bool { return r == ' ' || r == ',' })
	out := make([]uint32, 0, len(fields))
	for _, f := range fields {
		v, err := strconv.ParseUint(f, 10, 32)
		if err != nil {
			return nil, fmt.Errorf("invalid AS number %q", f)
		}
		out = append(out, uint32(v))
	}
	return out, nil
}
