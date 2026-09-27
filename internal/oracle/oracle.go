// Package oracle contains independent reference implementations used ONLY
// by tests. They deliberately do not import flexhash/internal/hashring:
//
//   - Affinity/flow hashing is re-derived with FNV-1a from scratch.
//   - Remainder allocation is the textbook Hamilton method, written
//     independently.
//   - Bucket assignment uses an independent formulation: instead of the
//     production global-pair greedy fill, each bucket is assigned in bucket
//     order to the member with highest affinity among members still needing
//     buckets. With sorted-bucket traversal this is a different algorithm
//     shape from production; where both agree it is genuine cross-validation,
//     not the implementation grading itself.
//   - ModN is the full-rehash baseline (owner = memberAt[hash(flow) mod n])
//     that the migration tests compare against.
package oracle

import (
	"hash/fnv"
	"math"
	"sort"
)

// FlowBucket is an independent FNV-1a flow->bucket mapping.
func FlowBucket(key string, n int) int {
	h := fnv.New64a()
	_, _ = h.Write([]byte("flow|" + key))
	return int(h.Sum64() % uint64(n))
}

// Affinity is an independent FNV-1a (bucket, member) affinity score.
func Affinity(bucket int, memberID string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte("b"))
	var buf [20]byte
	s := itoa(buf[:], int64(bucket))
	_, _ = h.Write(s)
	_, _ = h.Write([]byte("|m" + memberID))
	return h.Sum64()
}

// HamiltonQuota independently allocates integer bucket shares by largest
// remainder. Zero-weight members are omitted.
func HamiltonQuota(weights map[string]int, buckets int) map[string]int {
	total := 0
	ids := make([]string, 0, len(weights))
	for id, w := range weights {
		if w > 0 {
			total += w
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	out := map[string]int{}
	if total == 0 {
		return out
	}
	type c struct {
		id  string
		q   int
		rem float64
	}
	cs := make([]c, 0, len(ids))
	assigned := 0
	for _, id := range ids {
		exact := float64(buckets) * float64(weights[id]) / float64(total)
		base := int(math.Floor(exact))
		cs = append(cs, c{id, base, exact - float64(base)})
		out[id] = base
		assigned += base
	}
	rem := buckets - assigned
	sort.SliceStable(cs, func(i, j int) bool {
		if cs[i].rem != cs[j].rem {
			return cs[i].rem > cs[j].rem
		}
		return cs[i].id < cs[j].id
	})
	for k := 0; k < rem; k++ {
		out[cs[k%len(cs)].id]++
	}
	return out
}

// AssignOwners independently computes bucket ownership: buckets processed
// in ascending order, each goes to the still-unsatisfied member with the
// highest affinity. IDs ascending break ties.
func AssignOwners(weights map[string]int, buckets int) []string {
	quota := HamiltonQuota(weights, buckets)
	owner := make([]string, buckets)
	for b := 0; b < buckets; b++ {
		var bestID string
		var bestScore uint64
		found := false
		ids := make([]string, 0, len(quota))
		for id, q := range quota {
			if q > 0 {
				ids = append(ids, id)
			}
		}
		sort.Strings(ids)
		for _, id := range ids {
			s := Affinity(b, id)
			if !found || s > bestScore || (s == bestScore && id < bestID) {
				bestID, bestScore, found = id, s, true
			}
		}
		owner[b] = bestID
		quota[bestID]--
	}
	return owner
}

// ModN is the full-rehash baseline: with members sorted by ID, flow owner is
// members[hash(key) mod n]. Every member change can reassign up to all
// flows; this is the baseline the resilient ring must beat.
func ModN(key string, sortedIDs []string) string {
	if len(sortedIDs) == 0 {
		return ""
	}
	return sortedIDs[FlowBucket(key, len(sortedIDs))]
}

func itoa(buf []byte, v int64) []byte {
	if v == 0 {
		return append(buf[:0], '0')
	}
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	return buf[i:]
}
