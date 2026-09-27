package engine

import (
	"pathvector/internal/model"
)

// compare.go implements the supported best-path comparison order. Each step
// returns cmp<0 when a is strictly better than b; the first decisive step
// is reported as the selection reason so traces explain *why* a path won.
//
// Supported order (a wins when the noted quantity is better):
//
//  1. local-pref            higher
//  2. AS_PATH length        shorter (AS_SETs unsupported; one AS_SEQUENCE)
//  3. ORIGIN                IGP < EGP < INCOMPLETE
//  4. MED                   lower — only compared between candidates whose
//                            leftmost (neighboring) AS is the same; when the
//                            routes are MED-incomparable this step is skipped
//  5. source type           local origin > eBGP-learned > iBGP-learned
//  6. IGP cost to egress    lower
//  7. neighbor router id    lower (final deterministic tie-break)
//
// AS_PATH loop rejection (path contains the receiver's own ASN) happens
// before import policy and is not part of this comparison: a looping route
// is never admitted as a candidate.

type cmpStep struct {
	name string
	cmp  func(a, b *model.Candidate, ctx cmpCtx) int
}

type cmpCtx struct {
	idx      indexView
	receiver string
}

type indexView interface {
	ASN(id string) int
	SameAS(a, b string) bool
	IGPCost(id string) int
	Ordinal(id string) int
}

var cmpOrder = []cmpStep{
	{"local_pref", func(a, b *model.Candidate, _ cmpCtx) int {
		return -cmpUint32(a.Attrs.LocalPref, b.Attrs.LocalPref) // higher better
	}},
	{"as_path_length", func(a, b *model.Candidate, _ cmpCtx) int {
		return cmpInt(len(a.Attrs.ASPath), len(b.Attrs.ASPath))
	}},
	{"origin", func(a, b *model.Candidate, _ cmpCtx) int {
		return cmpUint32(uint32(a.Attrs.Origin), uint32(b.Attrs.Origin))
	}},
	{"med", func(a, b *model.Candidate, _ cmpCtx) int {
		if !medComparable(a, b) {
			return 0
		}
		return cmpUint32(a.Attrs.MED, b.Attrs.MED)
	}},
	{"source_type", func(a, b *model.Candidate, ctx cmpCtx) int {
		return cmpInt(sourceRank(a), sourceRank(b))
	}},
	{"igp_cost", func(a, b *model.Candidate, ctx cmpCtx) int {
		return cmpInt(ctx.idx.IGPCost(egressNode(a, ctx.receiver)),
			ctx.idx.IGPCost(egressNode(b, ctx.receiver)))
	}},
	{"router_id", func(a, b *model.Candidate, ctx cmpCtx) int {
		return cmpInt(ctx.idx.Ordinal(a.FromPeer), ctx.idx.Ordinal(b.FromPeer))
	}},
}

// medComparable implements the "same neighboring AS" rule: MED is compared
// only when the leftmost AS of both paths is identical. Local-origin routes
// have no neighboring AS and are never MED-comparable.
func medComparable(a, b *model.Candidate) bool {
	la, ea := leftmostAS(a)
	lb, eb := leftmostAS(b)
	if ea || eb {
		return false
	}
	return la == lb
}

// sourceRank ranks the learning source (lower better): local origin, then
// eBGP, then iBGP.
func sourceRank(c *model.Candidate) int {
	switch {
	case c.FromPeer == model.LocalOrigin:
		return 0
	case c.Attrs.LearnedIBGP:
		return 2
	default:
		return 1
	}
}

// egressNode maps a candidate to the topology node whose IGP cost counts.
// All iBGP sessions use next-hop-self (documented modeling choice), so a
// candidate's next-hop owner is the router it was learned from;
// local-origin candidates are anchored to the receiver itself.
func egressNode(c *model.Candidate, receiver string) string {
	if c.FromPeer == model.LocalOrigin {
		return receiver
	}
	return c.FromPeer
}

// selectBest chooses the best candidate and returns it plus the name of the
// step that decided the final comparison (for the trace). Callers must pass
// a non-empty slice.
func selectBest(cands []*model.Candidate, ctx cmpCtx) (*model.Candidate, string) {
	best := cands[0]
	reason := "only_candidate"
	for _, c := range cands[1:] {
		decided := ""
		winner := best
		for _, s := range cmpOrder {
			if d := s.cmp(c, best, ctx); d != 0 {
				decided = s.name
				if d < 0 {
					winner = c
				}
				break
			}
		}
		if winner == c {
			best = c
			if decided == "" {
				reason = "router_id"
			} else {
				reason = decided
			}
		}
	}
	return best, reason
}

// CompareTwo is the test-visible pairwise comparator: it returns the
// winning candidate and the decisive step name ("tie" if equal).
func CompareTwo(a, b *model.Candidate, ctx cmpCtx) (*model.Candidate, string) {
	for _, s := range cmpOrder {
		if d := s.cmp(a, b, ctx); d != 0 {
			if d < 0 {
				return a, s.name
			}
			return b, s.name
		}
	}
	return nil, "tie"
}

func leftmostAS(c *model.Candidate) (int, bool) {
	if c.FromPeer == model.LocalOrigin || len(c.Attrs.ASPath) == 0 {
		return 0, true
	}
	// AS_PATH is stored newest-first (export prepends the speaker ASN at
	// index 0), so index 0 is the directly neighboring AS.
	return c.Attrs.ASPath[0], false
}

func cmpUint32(a, b uint32) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

func cmpInt(a, b int) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}
