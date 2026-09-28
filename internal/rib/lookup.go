package rib

import (
	"fmt"

	"github.com/opp221/ribd/internal/netmodel"
)

// CandidateMatch is one candidate set encountered while matching an address
// against one resolution step, with the winning candidate of that prefix.
type CandidateMatch struct {
	Prefix       string         `json:"prefix"`
	Best         netmodel.Route `json:"best"`
	CandidateIDs []string       `json:"candidate_ids"`
	Chosen       bool           `json:"chosen"`
}

// ResolveHop is one step of recursive next-hop resolution. Step 1 matches the
// query address; each later step resolves the previous next hop.
type ResolveHop struct {
	Step    int              `json:"step"`
	Target  string           `json:"target"`
	Matches []CandidateMatch `json:"matches"`
	Chosen  *netmodel.Route  `json:"chosen,omitempty"`
	Via     *netmodel.Addr   `json:"via,omitempty"`
	Egress  string           `json:"egress,omitempty"`
}

// LookupResult is the full decision trace for one query.
type LookupResult struct {
	TableVersion uint64           `json:"table_version"`
	Target       string           `json:"target"`
	Family       string           `json:"family"`
	Status       Status           `json:"status"`
	Failure      Failure          `json:"failure,omitempty"`
	Reason       string           `json:"reason,omitempty"`
	MatchChain   []CandidateMatch `json:"match_chain"`
	ResolveChain []ResolveHop     `json:"resolve_chain"`
	NextHop      *netmodel.Addr   `json:"next_hop,omitempty"`
	Egress       string           `json:"egress,omitempty"`
	ChosenRoute  string           `json:"chosen_route,omitempty"`
}

// Lookup performs longest-prefix match plus recursive next-hop resolution for
// an address. The lookup is read-only over the snapshot.
func (s *Snapshot) Lookup(target netmodel.Addr) LookupResult {
	res := LookupResult{
		TableVersion: s.Version,
		Target:       target.String(),
		Family:       target.Family().String(),
		MatchChain:   []CandidateMatch{},
		ResolveChain: []ResolveHop{},
	}
	chain := s.match(target)
	res.MatchChain = chain
	if chosenCandidate(chain) == nil {
		res.Status = StatusIndeterminate
		res.Failure = FailureNoRoute
		res.Reason = fmt.Sprintf("no route in table v%d covers %s", s.Version, target)
		return res
	}
	s.resolve(target, &res)
	return res
}

// match returns the candidate match chain for an address, longest first,
// marking the first match (longest prefix) as chosen. Administrative distance
// never participates here: shorter prefixes are always shadowed by longer
// ones and remain visible in the trace.
func (s *Snapshot) match(target netmodel.Addr) []CandidateMatch {
	tree := treeFor(s, target.Family())
	hits, err := tree.MatchChain(target.Bytes())
	if err != nil {
		return []CandidateMatch{}
	}
	out := make([]CandidateMatch, 0, len(hits))
	for i, h := range hits {
		set := h.Value
		pfx := netmodel.PrefixFrom(target, h.KeyLen)
		ids := make([]string, 0, len(set.Entries))
		for _, e := range set.Entries {
			ids = append(ids, e.Route.ID)
		}
		out = append(out, CandidateMatch{
			Prefix:       pfx.String(),
			Best:         set.best().Route,
			CandidateIDs: ids,
			Chosen:       i == 0,
		})
	}
	return out
}

// resolve walks next hops from the first chosen candidate, detecting loops
// (self and multi-hop) and honoring the per-table depth limit.
func (s *Snapshot) resolve(start netmodel.Addr, res *LookupResult) {
	visited := map[string]int{} // route id -> step first visited
	target := start

	for step := 1; ; step++ {
		chain := s.match(target)
		hop := ResolveHop{Step: step, Target: target.String(), Matches: chain}

		chosen := chosenCandidate(chain)
		if chosen == nil {
			res.ResolveChain = append(res.ResolveChain, hop)
			res.Status = StatusIndeterminate
			res.Failure = FailureUnresolved
			res.Reason = fmt.Sprintf("next hop %s is not covered by any route; resolution stops at step %d (table v%d)", target, step, s.Version)
			return
		}
		hop.Chosen = &chosen.Route
		rid := chosen.Route.ID
		if step == 1 {
			res.ChosenRoute = rid
		}

		nh := chosen.Route.NextHop

		// Loop detection before any further recursion: a route whose next hop
		// equals the address being resolved is a self loop; revisiting a route
		// closes a cycle of length >= 2.
		if nh.IsRecursive() && nh.Addr == target {
			res.ResolveChain = append(res.ResolveChain, hop)
			res.Status = StatusRejected
			res.Failure = FailureLoop
			res.Reason = fmt.Sprintf("route %s (%s) points back at its own resolution target %s (self loop at step %d)",
				rid, chosen.Route.Prefix, target, step)
			return
		}
		if prev, seen := visited[rid]; seen {
			res.ResolveChain = append(res.ResolveChain, hop)
			res.Status = StatusRejected
			res.Failure = FailureLoop
			res.Reason = fmt.Sprintf("resolution cycle: route %s (%s) revisited at step %d, first seen at step %d",
				rid, chosen.Route.Prefix, step, prev)
			return
		}

		switch {
		case nh.IsDirect():
			hop.Egress = nh.Interface
			res.ResolveChain = append(res.ResolveChain, hop)
			final := target
			res.NextHop = &final // on-link; for step 1 this equals the target
			res.Egress = nh.Interface
			res.Status = StatusResolved
			res.Reason = fmt.Sprintf("resolved via connected route %s egressing %s at step %d (table v%d)",
				rid, nh.Interface, step, s.Version)
			return

		case nh.IsRecursive():
			if nh.Addr.Family() != target.Family() {
				res.ResolveChain = append(res.ResolveChain, hop)
				res.Status = StatusRejected
				res.Failure = FailureCrossFamily
				res.Reason = fmt.Sprintf("route %s next hop %s is %s while resolving %s target %s",
					rid, nh.Addr, nh.Addr.Family(), target.Family(), target)
				return
			}
			// MaxDepth bounds recursion edges: the initial lookup is step 1,
			// so recursion from step N to N+1 is allowed only for N <= depth.
			if step > s.MaxDepth {
				res.ResolveChain = append(res.ResolveChain, hop)
				res.Status = StatusRejected
				res.Failure = FailureDepthExceeded
				res.Reason = fmt.Sprintf("recursion depth exceeded limit %d at step %d resolving %s via %s (route %s)",
					s.MaxDepth, step, start, nh.Addr, rid)
				return
			}
			visited[rid] = step
			via := nh.Addr
			hop.Via = &via
			res.ResolveChain = append(res.ResolveChain, hop)
			target = nh.Addr
		}
	}
}

func chosenCandidate(chain []CandidateMatch) *Entry {
	for i := range chain {
		if chain[i].Chosen {
			return &Entry{Route: chain[i].Best}
		}
	}
	return nil
}

// LookupText parses target then looks it up; invalid text yields a rejected
// bad-query result instead of an error so callers get a structured outcome.
func (s *Snapshot) LookupText(target string) LookupResult {
	a, err := netmodel.ParseAddr(target)
	if err != nil {
		return LookupResult{
			TableVersion: s.Version,
			Target:       target,
			Status:       StatusRejected,
			Failure:      FailureBadQuery,
			Reason:       err.Error(),
			MatchChain:   []CandidateMatch{},
			ResolveChain: []ResolveHop{},
		}
	}
	return s.Lookup(a)
}
