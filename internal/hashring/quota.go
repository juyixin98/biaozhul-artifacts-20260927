package hashring

import (
	"fmt"
	"sort"

	"flexhash/internal/fherr"
)

// Integer rounding strategy — explicitly specified, not implicit:
//
// Bucket shares are allocated with the Hamilton/largest-remainder method:
//
//  1. exact[i] = bucketCount * weight[i] / totalWeight   (floating ideal)
//  2. quota[i] = floor(exact[i])                           (integer floor)
//  3. the remaining (bucketCount - sum quota) buckets are handed out one by
//     one to the largest fractional remainder, ties broken by member ID
//     ascending (fully deterministic, no map-iteration dependence).
//
// Guarantees:
//   - sum(quota) == bucketCount exactly when totalWeight > 0;
//   - |quota[i] - exact[i]| < 1 for every member (quota is one of the two
//     nearest integers to the ideal share);
//   - members with weight 0 get quota 0 and hold no buckets;
//   - deterministic regardless of map traversal order.
//
// Trade-off: Hamilton's method can exhibit the "population paradox" when
// bucketCount changes (a fast-growing member can lose a bucket). We do not
// resize bucketCount during a run (it is fixed by configuration and part of
// the topology identity), so this cannot occur in operation. Changing
// bucketCount is treated as a new topology and explicitly recomputed.

type memberWeight struct {
	id     string
	weight int
}

// allocateQuotas returns memberID -> bucket quota for positive-weight
// members. It panics-free returns an input-class error for empty input or
// non-positive total weight (the caller distinguishes "all weights zero"
// from other failures).
func allocateQuotas(weights []memberWeight, bucketCount int) (map[string]int, error) {
	const op = "hashring.allocateQuotas"
	if bucketCount < 1 {
		return nil, fherr.New(fherr.KindComputationFailed, op,
			fmt.Sprintf("bucketCount %d < 1", bucketCount))
	}
	if len(weights) == 0 {
		return nil, fherr.New(fherr.KindComputationFailed, op, "no members")
	}
	total := 0
	for _, w := range weights {
		if w.weight < 0 {
			return nil, fherr.New(fherr.KindComputationFailed, op,
				"negative weight for "+w.id)
		}
		total += w.weight
	}
	if total == 0 {
		return nil, fherr.New(fherr.KindInput, op,
			"total weight is zero: no member can hold bucket share")
	}
	if int64(total)*int64(bucketCount) > 1<<62 {
		return nil, fherr.New(fherr.KindComputationFailed, op,
			"weight*bucketCount exceeds integer safety bound")
	}

	type cand struct {
		id        string
		quota     int
		remainder float64
	}
	cs := make([]cand, len(weights))
	assigned := 0
	for i, w := range weights {
		exact := float64(bucketCount) * float64(w.weight) / float64(total)
		base := int(exact)
		cs[i] = cand{id: w.id, quota: base, remainder: exact - float64(base)}
		assigned += base
	}
	remaining := bucketCount - assigned
	if remaining < 0 {
		// Mathematically impossible (sum floors <= sum exact = bucketCount);
		// guard anyway as a computation-class invariant.
		return nil, fherr.New(fherr.KindComputationFailed, op,
			fmt.Sprintf("floor sum %d > bucketCount %d", assigned, bucketCount))
	}

	// Largest remainder; ties broken by ID ascending for determinism.
	order := make([]int, len(cs))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool {
		x, y := cs[order[a]], cs[order[b]]
		if x.remainder != y.remainder {
			return x.remainder > y.remainder
		}
		return x.id < y.id
	})
	for k := 0; k < remaining; k++ {
		cs[order[k%len(order)]].quota++
	}

	out := make(map[string]int, len(weights))
	for _, c := range cs {
		out[c.id] = c.quota
	}
	return out, nil
}
