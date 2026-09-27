package model

// SeedKind enumerates the only externally accepted event types. This
// backend never parses wire BGP: every input is a synthetic fixture event.
type SeedKind string

const (
	// SeedAnnounce injects (or replaces) the local-origin route for a
	// prefix at one router.
	SeedAnnounce SeedKind = "announce"
	// SeedWithdraw removes one neighbor-originated candidate at one
	// router; it must never touch candidates learned from other peers.
	SeedWithdraw SeedKind = "withdraw"
)

// SeedEvent is one synthetic UPDATE fixture event, ordered by Seq.
type SeedEvent struct {
	Seq      int      `json:"seq"`
	Kind     SeedKind `json:"kind"`
	RouterID string   `json:"router_id"`
	Prefix   string   `json:"prefix"`
	// Peer identifies the candidate to install (announce) or remove
	// (withdraw). Empty on an announce means local origin.
	Peer      string  `json:"peer,omitempty"`
	NextHop   string  `json:"next_hop,omitempty"`
	LocalPref *uint32 `json:"local_pref,omitempty"`
	MED       *uint32 `json:"med,omitempty"`
	ASPath    []int   `json:"as_path,omitempty"`
	Origin    string  `json:"origin,omitempty"`
}

// MessageKind is the internal propagation event kind.
type MessageKind string

const (
	MsgAnnounce MessageKind = "announce"
	MsgWithdraw MessageKind = "withdraw"
)

// Message is an internal directed UPDATE travelling over one session.
// Messages are never accepted from outside the engine; tests and the replay
// API can only inject SeedEvents.
type Message struct {
	Kind   MessageKind
	Prefix string
	From   string // advertising / withdrawing router
	To     string // receiving router
	// Snapshot carries the post-export attributes (announce only).
	Snap RouteSnapshot
}

// RouteSnapshot is the serializable on-the-wire view of a candidate.
type RouteSnapshot struct {
	Prefix      string `json:"prefix"`
	FromPeer    string `json:"from_peer"`
	NextHop     string `json:"next_hop"`
	LocalPref   uint32 `json:"local_pref"`
	ASPath      []int  `json:"as_path"`
	MED         uint32 `json:"med"`
	Origin      string `json:"origin"`
	LearnedIBGP bool   `json:"learned_ibgp"`
}

// Snapshot converts a candidate to its wire form.
func (c Candidate) Snapshot() RouteSnapshot {
	return RouteSnapshot{
		Prefix:      c.Prefix,
		FromPeer:    c.FromPeer,
		NextHop:     c.NextHop,
		LocalPref:   c.Attrs.LocalPref,
		ASPath:      append([]int(nil), c.Attrs.ASPath...),
		MED:         c.Attrs.MED,
		Origin:      c.Attrs.Origin.String(),
		LearnedIBGP: c.Attrs.LearnedIBGP,
	}
}

// ToCandidate parses a snapshot back into a candidate (origin is validated).
func (s RouteSnapshot) ToCandidate() (Candidate, error) {
	o, err := ParseOrigin(s.Origin)
	if err != nil {
		return Candidate{}, err
	}
	return Candidate{
		Prefix:   s.Prefix,
		FromPeer: s.FromPeer,
		NextHop:  s.NextHop,
		Attrs: Attrs{
			LocalPref:   s.LocalPref,
			ASPath:      append([]int(nil), s.ASPath...),
			MED:         s.MED,
			Origin:      o,
			LearnedIBGP: s.LearnedIBGP,
		},
	}, nil
}
