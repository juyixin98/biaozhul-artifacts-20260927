package scheduler

import (
	"fmt"
	"sort"

	"opp284/placement/internal/model"
)

// SearchLimits caps exhaustive enumeration.
type SearchLimits struct {
	MaxInstances   int
	MaxCandidates  int
	MaxLeafVisits  int
	IncludeEmptyDZ bool // include declared truly-empty zones in D*
}

// Normalize fills zero-valued limits with safe defaults.
func (l SearchLimits) Normalize() SearchLimits {
	if l.MaxInstances <= 0 {
		l.MaxInstances = 8
	}
	if l.MaxCandidates <= 0 {
		l.MaxCandidates = 12
	}
	if l.MaxLeafVisits <= 0 {
		l.MaxLeafVisits = 200000
	}
	return l
}

type dfsearch struct {
	snap   Snapshot
	req    Request
	idx    *index
	limits SearchLimits
	static map[string]staticCandidates
	order  []model.Intent

	steps *[]Step

	// best state (exhaustive mode only).
	bestScore *ScoreVector
	bestRes   *reservations
	bestDom   []string
	leafCount int
	truncated bool
	// deadEnds[intentID] holds one per-node reject snapshot per dead end.
	deadEnds map[string][][]model.HardReject
}

// blockedNodeFor returns the node that an intent may not reuse while its old
// copy is still running (rolling replacement, recreate disallowed).
func (d *dfsearch) blockedNodeFor(in model.Intent) string {
	if d.req.AllowRecreate {
		return ""
	}
	if rp, ok := d.req.Replacements[in.ID]; ok {
		if old, ok := d.idx.runningByID[rp.OldID]; ok {
			return old.NodeID
		}
	}
	return ""
}

// branchFilter is hardFilter plus rolling-replacement node blocking.
func (d *dfsearch) branchFilter(in model.Intent, res *reservations) ([]model.Node, []CandidateStep) {
	accepted, steps := hardFilter(in, d.idx, res)
	blocked := d.blockedNodeFor(in)
	return applyBlockedNode(accepted, blocked), applyBlockedStep(steps, blocked)
}

// orderIntents sorts intents most-constrained-first: pinned zones before
// unpinned, fewer static candidates first, then affinity groups (they restrict
// jointly), then id for determinism.
func orderIntents(intents []model.Intent, static map[string]staticCandidates) []model.Intent {
	out := append([]model.Intent(nil), intents...)
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		na, nb := static[a.ID].nodes, static[b.ID].nodes
		if (a.RequiredZone != "") != (b.RequiredZone != "") {
			return a.RequiredZone != ""
		}
		if len(na) != len(nb) {
			return len(na) < len(nb)
		}
		if len(a.AffinityGroups) != len(b.AffinityGroups) {
			return len(a.AffinityGroups) > len(b.AffinityGroups)
		}
		return a.ID < b.ID
	})
	return out
}

func (d *dfsearch) run() (*Decision, *model.PlanFailure) {
	d.limits = d.limits.Normalize()
	d.deadEnds = map[string][][]model.HardReject{}
	domains := participatingDomains(d.snap, d.idx, d.limits.IncludeEmptyDZ)

	exhaustive := len(d.req.Intents) <= d.limits.MaxInstances
	maxCand := 0
	for _, sc := range d.static {
		if len(sc.nodes) > maxCand {
			maxCand = len(sc.nodes)
		}
	}
	if maxCand > d.limits.MaxCandidates {
		exhaustive = false
	}

	*d.steps = append(*d.steps, Step{
		Stage:  "search_strategy",
		Detail: strategyDetail(exhaustive, maxCand),
	})

	if exhaustive {
		d.dfs(0, newReservations(), domains)
	} else {
		d.greedy(domains)
	}

	if d.truncated {
		*d.steps = append(*d.steps, Step{
			Stage:  "search_truncated",
			Detail: fmt.Sprintf("leaf visit cap %d reached; best-so-far assignment kept", d.limits.MaxLeafVisits),
		})
	}

	if d.bestRes == nil {
		return nil, d.buildFailure()
	}

	placements := make([]model.Placement, 0, len(d.order))
	for _, in := range d.order {
		nid := d.bestRes.nodeForInstance[in.ID]
		n := d.idx.nodeByID[nid]
		p := model.Placement{InstanceID: in.ID, NodeID: nid, Zone: n.Zone}
		if rp, ok := d.req.Replacements[in.ID]; ok {
			p.ReplacesID = rp.OldID
		}
		placements = append(placements, p)
	}
	sort.Slice(placements, func(i, j int) bool { return placements[i].InstanceID < placements[j].InstanceID })

	score := evaluate(d.snap, d.idx, d.bestRes, d.bestDom, d.req.Intents)
	*d.steps = append(*d.steps, Step{
		Stage:  "score_final",
		Detail: fmt.Sprintf("exhaustive=%v leaves=%d D*=%v", exhaustive, d.leafCount, domains),
		ScoreAfter: &score,
	})
	return &Decision{
		PlanID:               d.req.PlanID,
		Placements:           placements,
		Score:                score,
		Exhaustive:           exhaustive,
		LeafVisits:           d.leafCount,
		Steps:                *d.steps,
		ParticipatingDomains: domains,
	}, nil
}

func strategyDetail(exhaustive bool, maxCand int) string {
	if exhaustive {
		return fmt.Sprintf("exhaustive depth-first enumeration (max candidates %d)", maxCand)
	}
	return "size caps exceeded; deterministic most-constrained-first greedy fallback"
}

// dfs explores all feasible complete assignments.
func (d *dfsearch) dfs(depth int, res *reservations, domains []string) {
	if depth == len(d.order) {
		d.leafCount++
		score := evaluate(d.snap, d.idx, res, domains, d.req.Intents)
		if d.bestScore == nil || score.Less(*d.bestScore) {
			d.bestScore = &score
			d.bestRes = res.clone()
			d.bestDom = append([]string(nil), domains...)
			*d.steps = append(*d.steps, Step{
				Stage:      "leaf_improvement",
				Detail:     fmt.Sprintf("leaf #%d new best: range=%d varianceMilli=%d maxUtilMilli=%d", d.leafCount, score.ZoneCountRange, score.ZoneCountVariance, score.MaxNodeUtil),
				ScoreAfter: &score,
			})
		}
		return
	}
	in := d.order[depth]
	cands, candSteps := d.branchFilter(in, res)
	*d.steps = append(*d.steps, Step{
		Stage:      "branch_filter",
		InstanceID: in.ID,
		Detail:     fmt.Sprintf("depth=%d legal nodes with temporary reservations: %d", depth, len(cands)),
		Candidates: candSteps,
	})
	for _, n := range cands {
		if d.leafCount >= d.limits.MaxLeafVisits {
			d.truncated = true
			return
		}
		next := res.clone()
		d.apply(next, in, n.ID)
		d.dfs(depth+1, next, domains)
		if d.truncated {
			return
		}
	}
	if len(cands) == 0 {
		// Record the strongest reject seen at this dead end.
		d.recordDeadEnd(in, res)
	}
}

// greedy places intents sequentially; at each step it evaluates soft delta for
// every legal candidate and keeps the best (lexicographic on the FULL
// partial-assignment score), so simultaneous reservations still apply.
func (d *dfsearch) greedy(domains []string) {
	res := newReservations()
	ok := true
	for _, in := range d.order {
		cands, candSteps := d.branchFilter(in, res)
		*d.steps = append(*d.steps, Step{
			Stage:      "greedy_filter",
			InstanceID: in.ID,
			Detail:     fmt.Sprintf("legal nodes with temporary reservations: %d", len(cands)),
			Candidates: candSteps,
		})
		if len(cands) == 0 {
			d.recordDeadEnd(in, res)
			ok = false
			break
		}
		var bestNode model.Node
		var bestScore *ScoreVector
		for _, n := range cands {
			trial := res.clone()
			d.apply(trial, in, n.ID)
			placed := d.placedIntents(res)
			s := evaluate(d.snap, d.idx, trial, domains, placed)
			if bestScore == nil || s.Less(*bestScore) {
				bestScore = &s
				bestNode = n
			}
		}
		d.apply(res, in, bestNode.ID)
		*d.steps = append(*d.steps, Step{
			Stage:      "greedy_choose",
			InstanceID: in.ID,
			ChosenNode: bestNode.ID,
			Detail:     fmt.Sprintf("chose %s by lexicographic soft score", bestNode.ID),
			ScoreAfter: bestScore,
		})
	}
	if ok {
		d.leafCount = 1
		score := evaluate(d.snap, d.idx, res, domains, d.req.Intents)
		d.bestScore = &score
		d.bestRes = res
		d.bestDom = append([]string(nil), domains...)
	}
}

func (d *dfsearch) placedIntents(res *reservations) []model.Intent {
	out := make([]model.Intent, 0, len(res.nodeForInstance))
	for _, in := range d.order {
		if _, ok := res.nodeForInstance[in.ID]; ok {
			out = append(out, in)
		}
	}
	return out
}

func (d *dfsearch) apply(res *reservations, in model.Intent, nodeID string) {
	res.nodeForInstance[in.ID] = nodeID
	res.usage[nodeID] = res.usage[nodeID].Add(in.Request)
	for _, g := range d.idx.groupsForIntent[in.ID] {
		res.groupNodes[g.ID] = append(res.groupNodes[g.ID], nodeID)
	}
}

// recordDeadEnd captures, for a dead-end intent, the complete per-node
// rejects observed under the current reservations. Multiple dead ends for the
// same intent accumulate (different reservation prefixes can expose different
// binding reasons); the final classification takes a strict-precedence vote.
func (d *dfsearch) recordDeadEnd(in model.Intent, res *reservations) {
	blocked := d.blockedNodeFor(in)
	var all []model.HardReject
	for _, n := range d.idx.nodesSorted {
		free := n.Capacity.Sub(d.idx.held[n.ID]).Sub(res.usage[n.ID])
		rejects := nodeHardRejects(in, n, d.idx, res, free)
		if n.ID == blocked {
			rejects = append(rejects, model.HardReject{
				Code:   model.ReasonRollingBlocked,
				NodeID: blocked,
				Detail: "replacement must land elsewhere while the old copy still runs (allow_recreate=false)",
			})
		}
		all = append(all, rejects...)
	}
	if len(all) > 0 {
		d.deadEnds[in.ID] = append(d.deadEnds[in.ID], all)
	}
}
