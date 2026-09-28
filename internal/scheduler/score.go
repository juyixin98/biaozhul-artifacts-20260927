package scheduler

import (
	"math"
	"sort"
	"strings"

	"opp284/placement/internal/model"
)

// ScoreVector is the lexicographically-compared soft objective. Smaller is
// better; components are compared in order so that balancing instance counts
// across zones always dominates node-level balance, which dominates the
// utilization tiebreak. The last component is a deterministic assignment
// signature ensuring unique ordering regardless of exploration order.
type ScoreVector struct {
	// ZoneCountRange = max load - min load over D*, load = number of placed
	// (new) instances per participating zone.
	ZoneCountRange int `json:"zone_count_range"`
	// ZoneCountVariance = sum over D* of (load - mean)^2 * 1e3, rounded.
	// Captures "how spread" beyond the simple range (1,1,1,3 vs 0,2,2,2 share
	// range 2 but differ in variance).
	ZoneCountVariance int64 `json:"zone_count_variance_milli"`
	// MaxNodeUtil is the worst post-placement node utilization in thousandths
	// (max over nodes and resource dimensions of used/capacity*1000). Keeps a
	// workload off an already-hot node when zone skew is tied.
	MaxNodeUtil int64 `json:"max_node_util_milli"`
	// Signature makes the ordering total: node ids joined by instance order.
	Signature string `json:"signature"`
}

// Less implements strict lexicographic order.
func (a ScoreVector) Less(b ScoreVector) bool {
	if a.ZoneCountRange != b.ZoneCountRange {
		return a.ZoneCountRange < b.ZoneCountRange
	}
	if a.ZoneCountVariance != b.ZoneCountVariance {
		return a.ZoneCountVariance < b.ZoneCountVariance
	}
	if a.MaxNodeUtil != b.MaxNodeUtil {
		return a.MaxNodeUtil < b.MaxNodeUtil
	}
	return a.Signature < b.Signature
}

// participatingDomains computes D* (see config.SkewConfig):
//
//   - every zone containing at least one eligible node;
//   - if includeEmptyDeclared: zones in declaredZones with zero nodes.
//
// Zones that contain nodes but ALL of them are ineligible are excluded; a
// pinned request to one is caught as a hard failure elsewhere.
func participatingDomains(snap Snapshot, idx *index, includeEmptyDeclared bool) []string {
	set := map[string]bool{}
	for z := range idx.allEligibleZones {
		set[z] = true
	}
	if includeEmptyDeclared {
		for _, z := range snap.DeclaredZones {
			if !idx.existingZones[z] {
				set[z] = true
			}
		}
	}
	out := make([]string, 0, len(set))
	for z := range set {
		out = append(out, z)
	}
	sort.Strings(out)
	return out
}

// evaluate builds the ScoreVector for a complete tentative assignment.
//
// placements is ordered by instance id (the signature is evaluated in
// d.search order-independent of exploration order).
func evaluate(snap Snapshot, idx *index, res *reservations, domains []string, intentOrder []model.Intent) ScoreVector {
	// Zone instance counts from NEW placements only.
	zoneCount := map[string]int{}
	nodeForNew := map[string]string{}
	for _, in := range intentOrder {
		nid := res.nodeForInstance[in.ID]
		nodeForNew[in.ID] = nid
		if n, ok := idx.nodeByID[nid]; ok {
			zoneCount[n.Zone]++
		}
	}

	loads := make([]int, 0, len(domains))
	for _, z := range domains {
		loads = append(loads, zoneCount[z])
	}
	lo, hi := 0, 0
	if len(loads) > 0 {
		lo, hi = loads[0], loads[0]
		for _, v := range loads[1:] {
			if v < lo {
				lo = v
			}
			if v > hi {
				hi = v
			}
		}
	}
	mean := 0.0
	if len(loads) > 0 {
		sum := 0
		for _, v := range loads {
			sum += v
		}
		mean = float64(sum) / float64(len(loads))
	}
	var varianceMilli int64
	for _, v := range loads {
		d := float64(v) - mean
		varianceMilli += int64(math.Round(d * d * 1000))
	}

	// Post-placement node utilization: baseline held + temporary usage.
	var maxUtilMilli int64
	for _, n := range idx.nodesSorted {
		total := idx.held[n.ID].Add(res.usage[n.ID])
		for dim, cap := range n.Capacity {
			if cap <= 0 {
				continue
			}
			u := total[dim] * 1000 / cap
			if u > maxUtilMilli {
				maxUtilMilli = u
			}
		}
	}

	// Deterministic signature over intent-id order.
	sortedIntents := append([]model.Intent(nil), intentOrder...)
	sort.Slice(sortedIntents, func(i, j int) bool { return sortedIntents[i].ID < sortedIntents[j].ID })
	parts := make([]string, 0, len(sortedIntents))
	for _, in := range sortedIntents {
		parts = append(parts, in.ID+"="+nodeForNew[in.ID])
	}

	return ScoreVector{
		ZoneCountRange:    hi - lo,
		ZoneCountVariance: varianceMilli,
		MaxNodeUtil:       maxUtilMilli,
		Signature:         strings.Join(parts, "|"),
	}
}
