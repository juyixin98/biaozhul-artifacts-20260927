package scheduler

import (
	"sort"

	"placer/internal/model"
)

// Domain counting modes (requirement 2: make explicit whether empty domains
// and ineligible nodes count toward skew).
const (
	// modeConfigured counts every domain declared by the cluster config,
	// i.e. the union of domains across ALL nodes — including nodes that are
	// not ready or that fail this batch's selectors/taints.
	modeConfigured = "configured"
	// modeEligible counts only domains that contain at least one node
	// surviving the batch-wide status/zone/selector/taint pre-filter
	// (resources not considered — those are instance-specific).
	modeEligible = "eligible"
)

// domainSnapshot computes the load per spread domain for the current view
// under the configured counting policy.
//
// Load = number of occupants (bound + temporarily reserved) on nodes of
// that domain. Empty domains are included with load 0 whenever the policy
// counts them, so an empty eligible zone pulls future placements just like
// a sparse occupied zone.
func (v *view) domainSnapshot(pending []model.Instance) *model.SkewSnapshot {
	mode := v.options.SkewDomainMode
	if mode == "" {
		mode = modeConfigured
	}
	includeEmpty := true
	// IncludeEmptyDomains defaults to true; an explicit false is the only
	// way to disable it.
	if b := v.options.IncludeEmptyDomains; b != nil && !*b {
		includeEmpty = false
	}

	loads := map[string]int{}
	declared := map[string]bool{} // domains existing in cluster config
	hasEligibleNode := map[string]bool{}

	for _, st := range v.nodes {
		d, ok := st.n.DomainValue(v.key)
		if !ok {
			// Nodes lacking the spread key are themselves the missing-domain
			// case; they are listed as excluded, never silently counted.
			continue
		}
		declared[d] = true
		if st.eligible {
			hasEligibleNode[d] = true
		}
		if _, seen := loads[d]; !seen {
			loads[d] = 0
		}
		loads[d] += len(st.occup)
	}

	counted := map[string]bool{}
	switch mode {
	case modeEligible:
		for d := range hasEligibleNode {
			counted[d] = true
		}
	default: // configured
		for d := range declared {
			counted[d] = true
		}
	}
	if !includeEmpty {
		for d := range counted {
			if loads[d] == 0 {
				delete(counted, d)
			}
		}
	}

	snap := &model.SkewSnapshot{
		TopologyKey: v.key,
		Mode:        mode,
		Loads:       map[string]int{},
	}
	maxLoad, minLoad := 0, 0
	first := true
	for d := range counted {
		l := loads[d]
		snap.Loads[d] = l
		if first || l > maxLoad {
			maxLoad = l
		}
		if first || l < minLoad {
			minLoad = l
		}
		first = false
	}
	for d := range declared {
		if !counted[d] {
			snap.ExcludedDomains = append(snap.ExcludedDomains, d)
		}
	}
	sort.Strings(snap.ExcludedDomains)
	for d := range snap.Loads {
		snap.CountedDomains = append(snap.CountedDomains, d)
	}
	sort.Strings(snap.CountedDomains)
	snap.MaxLoad = maxLoad
	snap.MinLoad = minLoad
	snap.Skew = maxLoad - minLoad
	return snap
}

// hypotheticalSkew returns the skew after adding one occupant to the given
// node without mutating the view.
func (v *view) hypotheticalSkew(st *nodeState, pending []model.Instance) (skew, sumSquares int) {
	loads := map[string]int{}
	counted := v.countedDomainSet(pending)
	for _, s := range v.nodes {
		d, ok := s.n.DomainValue(v.key)
		if !ok || !counted[d] {
			continue
		}
		loads[d] += len(s.occup)
	}
	d, ok := st.n.DomainValue(v.key)
	if ok && counted[d] {
		loads[d]++
	}
	max, min := 0, 0
	first := true
	for _, l := range loads {
		sumSquares += l * l
		if first || l > max {
			max = l
		}
		if first || l < min {
			min = l
		}
		first = false
	}
	return max - min, sumSquares
}

func (v *view) countedDomainSet(pending []model.Instance) map[string]bool {
	mode := v.options.SkewDomainMode
	if mode == "" {
		mode = modeConfigured
	}
	set := map[string]bool{}
	for _, st := range v.nodes {
		d, ok := st.n.DomainValue(v.key)
		if !ok {
			continue
		}
		if mode == modeEligible && !st.eligible {
			continue
		}
		set[d] = true
	}
	if b := v.options.IncludeEmptyDomains; b != nil && !*b {
		for d := range set {
			cnt := 0
			for _, st := range v.nodes {
				if dd, ok := st.n.DomainValue(v.key); ok && dd == d {
					cnt += len(st.occup)
				}
			}
			if cnt == 0 {
				delete(set, d)
			}
		}
	}
	return set
}

// softGroupViolations counts soft-rule violations introduced by placing
// inst on st (read-only). A violation is a pair (inst, existing occupant)
// that a soft rule would forbid; the score steers away from such nodes
// without ever making them illegal.
func (v *view) softGroupViolations(inst model.Instance, st *nodeState) int {
	domain, domainOK := st.n.DomainValue(v.key)
	count := 0
	for i, r := range v.gi.rules {
		if r.Mode != model.ModeSoft {
			continue
		}
		val, participates := inst.Groups[r.Group]
		if !participates || !domainOK {
			continue
		}
		for _, other := range v.gi.members[i][val] {
			if other == inst.ID {
				continue
			}
			od := v.gi.domainsOf[i][other]
			if !r.Affinity && od == domain {
				count++
			}
			if r.Affinity && od != "" && od != domain {
				count++
			}
		}
	}
	return count
}

// softTaints returns the number of untolerated prefer_no_schedule taints.
func softTaints(inst model.Instance, st *nodeState) int {
	_, soft := st.n.ToleratesAll(inst.Tolerations)
	return soft
}

// scoreNode ranks a legal node for inst. Smaller tuples are better; the
// order is skew, load sum-of-squares (stable tie-break), soft group
// violations, untolerated soft taints, node id.
func (v *view) scoreNode(inst model.Instance, st *nodeState, pending []model.Instance) model.NodeScore {
	skew, sq := v.hypotheticalSkew(st, pending)
	return model.NodeScore{
		Skew:       skew,
		SumSquares: sq,
		SoftGroups: v.softGroupViolations(inst, st) + softTaints(inst, st),
	}
}

func scoreLess(a, b model.NodeScore, idA, idB string) bool {
	if a.Skew != b.Skew {
		return a.Skew < b.Skew
	}
	if a.SumSquares != b.SumSquares {
		return a.SumSquares < b.SumSquares
	}
	if a.SoftGroups != b.SoftGroups {
		return a.SoftGroups < b.SoftGroups
	}
	return idA < idB
}
