// Package ring implements the weighted consistent-hash ring and, crucially,
// keeps two different notions of "share" apart:
//
//   - Bucket share: the fraction of the 64-bit hash circle a member's virtual
//     nodes own. With B vnodes of V total, share = B/V. This is what moves
//     minimally when membership changes.
//   - Actual traffic share: the fraction of a concrete, finite flow set that
//     lands on a member. It is a Monte-Carlo realization of bucket share over
//     the flow keys' hash values, and only equals B/V in the limit. For any
//     specific flow corpus it can deviate, and this package reports both.
//
// Minimal-migration guarantee: vnode placement depends only on (memberID,
// replicaIndex). Adding a member inserts new vnodes without moving existing
// ones, so only buckets immediately preceding a new vnode change owner;
// deleting a member hands its buckets to the still-present successor. Flows
// whose owner stays present never move. This is the property the test suite
// contrasts against a full modulo (hash % N) baseline.
package ring

import (
	"math"
	"sort"

	"flowrouter/internal/apperr"
	"flowrouter/internal/hashx"
)

// Member is a ring candidate after configuration parsing.
type Member struct {
	ID     string
	Weight int
}

// Allocation is the deterministic integer result of turning weights into
// vnode counts. All fields are exported for audit/logging.
type Allocation struct {
	Counts     map[string]int // member ID -> total vnode count (base + extra)
	Base       map[string]int // member ID -> guaranteed vnodes (capped mode)
	Extra      map[string]int // member ID -> vnodes from proportional remainder stage
	Total      int            // sum of Counts (ring size)
	Ideal      map[string]float64
	Remainder  map[string]float64
	Strategy   string // "linear_weight", "largest_remainder" or "empty"
	VNodesPerW int
	CappedTo   int // 0 when uncapped
}

// AllocateVNodes converts positive integer weights into integer vnode counts.
//
// Strategy:
//
//  1. Members with weight <= 0 get exactly 0 vnodes (tracked elsewhere in the
//     router, but absent from the ring).
//
//  2. Uncapped (cap <= 0): count = weight * vnodesPerWeight. Exact integer
//     multiplication — no rounding occurs; the declared ring is fully
//     proportional with granularity vnodesPerWeight.
//
//  3. Capped (cap > 0): the total count must equal cap while every positive
//     member still receives >= 1 vnode. A two-stage largest-remainder policy
//     is used:
//
//     a) reserve one vnode per positive member (stage "base");
//     b) distribute the remaining B = cap - n vnodes in proportion to weight:
//     each member gets floor(B*w/W); the B - Σfloor leftover vnodes are handed
//     out one at a time by largest fractional remainder of B*w/W, ties broken
//     by ascending member ID (stage "remainder").
//
//     This always totals exactly cap and is feasible whenever cap >= number of
//     positive members. The proportional ideal cap*w/W and the remainder used
//     in stage (b) are both recorded on Allocation for audit. Note the base
//     stage intentionally over-represents very light members: a hard cap plus
//     a hard presence minimum cannot be perfectly proportional, and the
//     trade-off is explicit.
//
// Edge cases:
//   - no positive members -> empty allocation, Strategy "largest_remainder"
//     is not used; Total 0, Strategy "empty".
//   - cap smaller than the number of positive members is rejected at config
//     validation time; defense in depth returns COMPUTATION_FAILURE here.
//   - overflow of weight*vnodesPerWeight is reported as COMPUTATION_FAILURE.
func AllocateVNodes(members []Member, vnodesPerWeight, cap_ int) (*Allocation, error) {
	if vnodesPerWeight < 1 {
		return nil, apperr.Compute("BAD_VNODES_PER_WEIGHT", "vnodesPerWeight must be >= 1")
	}
	ids := make([]string, 0, len(members))
	weights := make(map[string]int, len(members))
	totalW := 0
	for _, m := range members {
		if m.Weight <= 0 {
			continue
		}
		ids = append(ids, m.ID)
		weights[m.ID] = m.Weight
		totalW += m.Weight
	}
	sort.Strings(ids) // canonical processing/tie-break order

	a := &Allocation{
		Counts:     map[string]int{},
		Ideal:      map[string]float64{},
		Remainder:  map[string]float64{},
		VNodesPerW: vnodesPerWeight,
		CappedTo:   cap_,
	}
	if len(ids) == 0 {
		a.Strategy = "empty"
		return a, nil
	}

	if cap_ <= 0 {
		a.Strategy = "linear_weight"
		for _, id := range ids {
			w := weights[id]
			// Guard 64-bit multiplication overflow.
			if w > math.MaxInt/vnodesPerWeight {
				return nil, apperr.Compute("VNODE_COUNT_OVERFLOW",
					"weight*vnodes_per_weight overflows for member "+id)
			}
			c := w * vnodesPerWeight
			a.Counts[id] = c
			a.Total += c
			a.Ideal[id] = float64(w) // raw ideal before normalization
			a.Remainder[id] = 0
		}
		return a, nil
	}

	if cap_ < len(ids) {
		return nil, apperr.Compute("CAP_BELOW_MEMBER_COUNT",
			"max vnodes smaller than number of positive-weight members")
	}
	a.Strategy = "largest_remainder"
	a.Base = map[string]int{}
	a.Extra = map[string]int{}

	n := len(ids)
	remBudget := cap_ - n // vnodes left after the one-per-member base
	type cand struct {
		id  string
		flr int
		rem float64
	}
	cands := make([]cand, 0, n)
	floorSum := 0
	for _, id := range ids {
		// Unconstrained proportional ideal and realized extra within remBudget.
		ideal := float64(cap_) * float64(weights[id]) / float64(totalW)
		extraExact := float64(remBudget) * float64(weights[id]) / float64(totalW)
		flr := int(extraExact)
		rem := extraExact - float64(flr)
		a.Ideal[id] = ideal
		a.Remainder[id] = rem
		a.Base[id] = 1
		a.Counts[id] = 1 + flr
		floorSum += flr
		cands = append(cands, cand{id: id, flr: flr, rem: rem})
	}
	// Hand out remaining vnodes by largest fractional remainder; tie by ID.
	leftover := remBudget - floorSum
	sort.SliceStable(cands, func(i, j int) bool {
		if cands[i].rem != cands[j].rem {
			return cands[i].rem > cands[j].rem
		}
		return cands[i].id < cands[j].id
	})
	for k := 0; k < leftover; k++ {
		cands[k%len(cands)].flr++
	}
	for _, c := range cands {
		a.Extra[c.id] = c.flr
		a.Counts[c.id] = a.Base[c.id] + c.flr
	}
	a.Total = cap_
	return a, nil
}

// VNode is one point on the ring.
type VNode struct {
	Hash     uint64
	MemberID string
	Replica  int
}

// Ring is an immutable, fully constructed hash ring. Construct a new one for
// every membership/weight/health version; routers swap pointers atomically.
type Ring struct {
	vnodes    []VNode // sorted by Hash, then MemberID, then Replica
	memberSet map[string]bool
	alloc     *Allocation
}

// Build places vnodes per alloc and sorts the ring. Duplicate hashes (possible
// in principle across different member/replica inputs) are kept and resolved
// deterministically in Lookup by the same sort tie-break (member ID, replica).
func Build(members []Member, vnodesPerWeight, cap_ int) (*Ring, error) {
	alloc, err := AllocateVNodes(members, vnodesPerWeight, cap_)
	if err != nil {
		return nil, err
	}
	vs := make([]VNode, 0, alloc.Total)
	for _, m := range members {
		c := alloc.Counts[m.ID]
		for r := 0; r < c; r++ {
			vs = append(vs, VNode{
				Hash:     hashx.VNodeHash(m.ID, r),
				MemberID: m.ID,
				Replica:  r,
			})
		}
	}
	sort.Slice(vs, func(i, j int) bool {
		if vs[i].Hash != vs[j].Hash {
			return vs[i].Hash < vs[j].Hash
		}
		if vs[i].MemberID != vs[j].MemberID {
			return vs[i].MemberID < vs[j].MemberID
		}
		return vs[i].Replica < vs[j].Replica
	})
	set := make(map[string]bool, len(members))
	for _, m := range members {
		set[m.ID] = true
	}
	return &Ring{vnodes: vs, memberSet: set, alloc: alloc}, nil
}

// Empty reports whether the ring has no owner (no positive-weight, included
// members). Callers route this to NO_HEALTHY_MEMBER.
func (r *Ring) Empty() bool { return len(r.vnodes) == 0 }

// Allocation exposes the integer allocation audit record.
func (r *Ring) Allocation() *Allocation { return r.alloc }

// Len is the vnode count.
func (r *Ring) Len() int { return len(r.vnodes) }

// HasMember reports membership by ID (including weight-zero members, which
// appear in memberSet but own no vnodes).
func (r *Ring) HasMember(id string) bool { return r.memberSet[id] }

// Lookup returns the owner of a flow hash: the first vnode at or after the
// hash on the wrapped circle. Binary search over the sorted ring; the modulus
// wrap gives the "next-hop" semantics. Returns ("", false) on an empty ring.
func (r *Ring) Lookup(flowHash uint64) (string, bool) {
	n := len(r.vnodes)
	if n == 0 {
		return "", false
	}
	i := sort.Search(n, func(i int) bool { return r.vnodes[i].Hash >= flowHash })
	if i == n {
		i = 0
	}
	return r.vnodes[i].MemberID, true
}

// BucketShare returns the exact ring-arc share owned by a member:
// count(member)/total vnodes. This is a bucket share, not a traffic share.
func (r *Ring) BucketShare(memberID string) (float64, bool) {
	if r.alloc.Total == 0 {
		return 0, false
	}
	c, ok := r.alloc.Counts[memberID]
	if !ok {
		return 0, false
	}
	return float64(c) / float64(r.alloc.Total), true
}

// VNodeCounts returns a copy of the per-member vnode counts.
func (r *Ring) VNodeCounts() map[string]int {
	out := make(map[string]int, len(r.alloc.Counts))
	for k, v := range r.alloc.Counts {
		out[k] = v
	}
	return out
}

// VNodes returns a copy of the sorted vnode slice (used by diagnostics and by
// the replay comparison API; routing uses Lookup).
func (r *Ring) VNodes() []VNode {
	out := make([]VNode, len(r.vnodes))
	copy(out, r.vnodes)
	return out
}
