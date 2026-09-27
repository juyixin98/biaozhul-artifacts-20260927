package hashring

import (
	"sort"

	"flexhash/internal/fherr"
)

// Member is the routing-core view of one next hop.
type Member struct {
	ID      string `json:"id"`
	Address string `json:"address"`
	Weight  int    `json:"weight"`
	Healthy bool   `json:"healthy"`
}

// Assignment records who holds one bucket in the topology.
type Assignment struct {
	Bucket  int
	Member  string
	Version int64 // config version at which this bucket last moved
}

// Ring is an immutable snapshot of topology + bucket ownership + health.
// Lookups are lock-free reads of the slice fields; configuration changes
// produce a new Ring and swap it in atomically in Manager (see manager.go).
type Ring struct {
	Version     int64
	BucketCount int
	Members     map[string]Member
	// owner[b] = member that structurally owns bucket b (never empty for
	// positive total weight; zero-weight members never own buckets).
	owner []string
}

// newEmptyRing is used only for bootstrap; Version 0 means "uninitialized".
func newEmptyRing() *Ring {
	return &Ring{Version: 0, Members: map[string]Member{}}
}

// buildRing computes quota and ownership for a member set.
//
// With old == nil all buckets are newly assigned. Otherwise ownership is
// migrated minimally; bucketCount must match old's.
func buildRing(version int64, bucketCount int, members map[string]Member, old *Ring) (*Ring, []int, error) {
	const op = "hashring.buildRing"
	if old != nil && old.BucketCount != bucketCount {
		return nil, nil, fherr.New(fherr.KindComputationFailed, op,
			"bucket count changed; caller must perform a full recompute")
	}
	positive := make([]memberWeight, 0, len(members))
	totalWeight := 0
	for _, m := range members {
		if m.Weight > 0 {
			positive = append(positive, memberWeight{m.ID, m.Weight})
			totalWeight += m.Weight
		}
	}
	if totalWeight == 0 {
		return nil, nil, fherr.New(fherr.KindInput, "hashring.buildRing",
			"total weight is zero: no member can hold bucket share")
	}
	sort.Slice(positive, func(i, j int) bool { return positive[i].id < positive[j].id })

	quota, err := allocateQuotas(positive, bucketCount)
	if err != nil {
		return nil, nil, err
	}

	var owner []string
	var moved []int
	if old == nil {
		owner, err = assignGreedy(bucketCount, quota, nil)
		if err != nil {
			return nil, nil, err
		}
		moved = make([]int, 0, bucketCount)
		for b := 0; b < bucketCount; b++ {
			moved = append(moved, b)
		}
	} else {
		owner, moved, err = reassign(bucketCount, quota, old)
		if err != nil {
			return nil, nil, err
		}
	}

	r := &Ring{
		Version:     version,
		BucketCount: bucketCount,
		Members:     members,
		owner:       owner,
	}
	return r, moved, nil
}

// assignGreedy fills free buckets by global descending affinity: it generates
// every (free bucket, member with remaining need) pair, sorts by score
// descending with (memberID, bucket) tie-break, and accepts a pair only while
// the bucket is still free and the member still needs buckets.
//
// This shared mechanism backs both initial placement and migration. With
// needs summing to the free-bucket count it always saturates: the first free
// buckets are accepted for the members that want them most, and every member
// with a positive need appears as a candidate for every still-free bucket, so
// the scan cannot leave capacity unused once a member is satisfied.
func assignGreedy(bucketCount int, need map[string]int, owner []string) ([]string, error) {
	const op = "hashring.assignGreedy"
	if owner == nil {
		owner = make([]string, bucketCount)
	} else if len(owner) != bucketCount {
		return nil, fherr.New(fherr.KindComputationFailed, op, "owner slice length mismatch")
	}

	type pair struct {
		bucket int
		member string
		score  uint64
	}
	free := 0
	pairs := make([]pair, 0)
	for b := 0; b < bucketCount; b++ {
		if owner[b] != "" {
			continue
		}
		free++
		for id, q := range need {
			if q <= 0 {
				continue
			}
			pairs = append(pairs, pair{b, id, affinity(b, id)})
		}
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].score != pairs[j].score {
			return pairs[i].score > pairs[j].score
		}
		if pairs[i].member != pairs[j].member {
			return pairs[i].member < pairs[j].member
		}
		return pairs[i].bucket < pairs[j].bucket
	})

	remaining := make(map[string]int, len(need))
	for id, q := range need {
		remaining[id] = q
	}
	assigned := 0
	for _, p := range pairs {
		if assigned == free {
			break
		}
		if owner[p.bucket] != "" || remaining[p.member] <= 0 {
			continue
		}
		owner[p.bucket] = p.member
		remaining[p.member]--
		assigned++
	}
	if assigned != free {
		return nil, fherr.New(fherr.KindComputationFailed, op,
			"greedy assignment failed saturation")
	}
	return owner, nil
}

// reassign preserves every bucket whose old owner survives, frees the rest
// (and any surplus of survivors whose quota shrank), then fills the free
// buckets to match quota via the shared greedy rule.
//
// Survivors above their new quota release the buckets with the LOWEST
// affinity to themselves — the symmetric counterpart of greedy fill, which
// prefers highest affinity: each member keeps the buckets it wants most.
func reassign(bucketCount int, quota map[string]int, old *Ring) ([]string, []int, error) {
	const op = "hashring.reassign"
	owner := make([]string, bucketCount)

	retainedBy := map[string][]int{}
	for b := 0; b < bucketCount; b++ {
		om := old.owner[b]
		if _, ok := quota[om]; ok { // survivor with positive weight
			retainedBy[om] = append(retainedBy[om], b)
		}
		// Removed members and members whose weight dropped to zero are
		// absent from quota -> their buckets stay free.
	}
	for id, bs := range retainedBy {
		q := quota[id]
		if len(bs) <= q {
			for _, b := range bs {
				owner[b] = id
			}
			continue
		}
		sort.Slice(bs, func(i, j int) bool {
			si, sj := affinity(bs[i], id), affinity(bs[j], id)
			if si != sj {
				return si < sj // lowest affinity first => first entries freed
			}
			return bs[i] < bs[j]
		})
		for _, b := range bs[len(bs)-q:] {
			owner[b] = id
		}
	}

	held := make(map[string]int, len(quota))
	for _, o := range owner {
		if o != "" {
			held[o]++
		}
	}
	need := make(map[string]int, len(quota))
	for id, q := range quota {
		if q > held[id] {
			need[id] = q - held[id]
		} else if q < held[id] {
			return nil, nil, fherr.New(fherr.KindComputationFailed, op,
				"member "+id+" retained above quota after surplus release")
		}
	}

	if _, err := assignGreedy(bucketCount, need, owner); err != nil {
		return nil, nil, err
	}

	moved := make([]int, 0)
	for b := 0; b < bucketCount; b++ {
		if old.owner[b] != owner[b] {
			moved = append(moved, b)
		}
	}
	return owner, moved, nil
}

// ---- immutable snapshot accessors (safe for concurrent lock-free reads) ----

// BucketOf maps a canonical flow key to its structural bucket index.
func (r *Ring) BucketOf(flowKey string) int {
	return hashFlow(flowKey, r.BucketCount)
}

// Owner returns the structural owner of a bucket ("" if none).
func (r *Ring) Owner(bucket int) string {
	if bucket < 0 || bucket >= len(r.owner) {
		return ""
	}
	return r.owner[bucket]
}

// Assignments returns a copy of the full bucket assignment table.
func (r *Ring) Assignments() []Assignment {
	out := make([]Assignment, len(r.owner))
	for b, m := range r.owner {
		out[b] = Assignment{Bucket: b, Member: m, Version: r.Version}
	}
	return out
}

// Quota counts structural buckets per member (zero-weight -> 0).
func (r *Ring) Quota() map[string]int {
	out := make(map[string]int, len(r.Members))
	for id := range r.Members {
		out[id] = 0
	}
	for _, m := range r.owner {
		if m != "" {
			out[m]++
		}
	}
	return out
}
