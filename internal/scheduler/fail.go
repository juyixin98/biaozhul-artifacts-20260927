package scheduler

import (
	"fmt"
	"sort"

	"opp284/placement/internal/model"
)

// reasonRank orders the hard-constraint pipeline stages. A node that fails an
// early stage never "reaches" a later one, so a structurally-ineligible node
// must not mask an anti-affinity conflict observed on eligible nodes.
//
// Pipeline: zone existence -> eligibility -> selector/zone-pin ->
// affinity/anti-affinity -> rolling-block -> resource capacity.
func reasonRank(k model.ReasonKind) int {
	switch k {
	case model.ReasonMissingDomain:
		return 0
	case model.ReasonNoNodesInCluster:
		return 1
	case model.ReasonNoEligibleNode:
		return 2
	case model.ReasonDomainNoEligibleNode:
		return 3
	case model.ReasonZoneMismatch:
		return 4
	case model.ReasonSelectorNoMatch:
		return 5
	case model.ReasonAffinityConflict, model.ReasonAntiAffinityConflict:
		return 6
	case model.ReasonRollingBlocked:
		return 7
	case model.ReasonInsufficientResource:
		return 8
	default:
		return 100
	}
}

// classifyStaticFailure handles intents whose depth-zero candidate set is
// empty — deterministically unschedulable regardless of joint assignment.
func classifyStaticFailure(req Request, snap Snapshot, static map[string]staticCandidates) *model.PlanFailure {
	var failed []model.InstanceFailure
	for _, in := range req.Intents {
		sc := static[in.ID]
		if len(sc.nodes) > 0 {
			continue
		}
		cat := pipelineCategory(in, snap, sc.rejected)
		failed = append(failed, model.InstanceFailure{
			InstanceID: in.ID,
			Reasons:    withCategory(cat, flattenRejects(sc.rejected)),
		})
	}
	if len(failed) == 0 {
		return nil
	}
	sort.Slice(failed, func(i, j int) bool { return failed[i].InstanceID < failed[j].InstanceID })
	return &model.PlanFailure{
		Code: model.ReasonConstraintConflict,
		Message: fmt.Sprintf("%d instance(s) cannot be placed: hard constraints eliminate every candidate",
			len(failed)),
		Instances: failed,
	}
}

// pipelineStage is one stage of the hard-constraint pipeline.
type pipelineStage struct {
	rank  int
	// codes dying at this stage may map to (representative category chosen
	// from observed rejects).
}

// pipelineCategory derives an instance failure category from observed per-node
// rejects using the staged-pipeline rule:
//
//  1. (pinned only) no in-zone node exists        -> MISSING_DOMAIN;
//  2. walk eligibility -> selector -> affinity -> rolling -> resource;
//     at each stage discard nodes carrying a reject at or before that stage;
//     the FIRST stage at which a non-empty survivor set becomes empty names
//     the category. Nodes dying earlier never vote at later stages.
//
// This makes "one eligible node blocked by mutual anti-affinity" classify as
// ANTI_AFFINITY_CONFLICT even though every other node is cordoned.
func pipelineCategory(in model.Intent, snap Snapshot, rejected map[string][]model.HardReject) model.ReasonKind {
	universe := make([]model.Node, 0, len(snap.Nodes))
	for _, n := range snap.Nodes {
		if in.RequiredZone == "" || n.Zone == in.RequiredZone {
			universe = append(universe, n)
		}
	}
	if in.RequiredZone != "" && len(universe) == 0 {
		return model.ReasonMissingDomain
	}

	// minRank per node across its observed rejects (no rejects => survives all).
	minRank := map[string]int{}
	codesAt := map[int][]model.ReasonKind{}
	for _, n := range universe {
		rs := rejected[n.ID]
		if len(rs) == 0 {
			minRank[n.ID] = 1 << 30
			continue
		}
		mr := reasonRank(rs[0].Code)
		for _, r := range rs[1:] {
			if rk := reasonRank(r.Code); rk < mr {
				mr = rk
			}
		}
		minRank[n.ID] = mr
		codesAt[mr] = append(codesAt[mr], rejectCodeAt(rs, mr))
	}

	stages := []int{2, 5, 6, 7, 8}
	categoryAt := map[int]model.ReasonKind{
		2: model.ReasonNoEligibleNode,
		5: model.ReasonSelectorNoMatch,
		7: model.ReasonRollingBlocked,
		8: model.ReasonInsufficientResource,
	}
	if in.RequiredZone != "" {
		categoryAt[2] = model.ReasonDomainNoEligibleNode
	}

	survivors := len(universe)
	for _, st := range stages {
		next := 0
		for _, n := range universe {
			if minRank[n.ID] > st {
				next++
			}
		}
		if survivors > 0 && next == 0 {
			if st == 6 {
				return dominantCode(codesAt[6], model.ReasonAntiAffinityConflict)
			}
			return categoryAt[st]
		}
		survivors = next
	}
	// Inconsistent state: candidate set empty but a node appears to survive —
	// report internal rather than a false success/category.
	return model.ReasonInternal
}

func rejectCodeAt(rs []model.HardReject, rank int) model.ReasonKind {
	for _, r := range rs {
		if reasonRank(r.Code) == rank {
			return r.Code
		}
	}
	return model.ReasonInternal
}

// dominantCode picks the most frequent stage-6 code; ties resolved by lexical
// order for determinism.
func dominantCode(codes []model.ReasonKind, fallback model.ReasonKind) model.ReasonKind {
	count := map[model.ReasonKind]int{}
	for _, c := range codes {
		if c == model.ReasonAffinityConflict || c == model.ReasonAntiAffinityConflict {
			count[c]++
		}
	}
	if len(count) == 0 {
		return fallback
	}
	best := fallback
	bestN := -1
	for c, n := range count {
		if n > bestN || (n == bestN && string(c) < string(best)) {
			best, bestN = c, n
		}
	}
	return best
}

func flattenRejects(rejected map[string][]model.HardReject) []model.HardReject {
	nodeIDs := make([]string, 0, len(rejected))
	for id := range rejected {
		nodeIDs = append(nodeIDs, id)
	}
	sort.Strings(nodeIDs)
	var out []model.HardReject
	for _, id := range nodeIDs {
		rs := append([]model.HardReject(nil), rejected[id]...)
		sort.SliceStable(rs, func(a, b int) bool { return reasonRank(rs[a].Code) < reasonRank(rs[b].Code) })
		out = append(out, rs...)
	}
	return out
}

func withCategory(cat model.ReasonKind, observed []model.HardReject) []model.HardReject {
	head := model.HardReject{Code: cat, NodeID: "*", Detail: categoryDetail(cat)}
	return append([]model.HardReject{head}, observed...)
}

func categoryDetail(k model.ReasonKind) string {
	switch k {
	case model.ReasonMissingDomain:
		return "pinned zone does not exist in the cluster"
	case model.ReasonDomainNoEligibleNode:
		return "pinned zone exists but every node in it is ineligible"
	case model.ReasonNoEligibleNode:
		return "every cluster node is ineligible (cordoned/maintenance)"
	case model.ReasonSelectorNoMatch:
		return "no eligible node satisfies the label selector"
	case model.ReasonAffinityConflict:
		return "affinity group members cannot all share one legal node"
	case model.ReasonAntiAffinityConflict:
		return "anti-affinity group members are forced onto the same node"
	case model.ReasonRollingBlocked:
		return "replacement cannot land on any other node while the old copy still runs"
	case model.ReasonInsufficientResource:
		return "no node has enough free capacity even after satisfying eligibility/selector/group constraints"
	case model.ReasonZoneMismatch:
		return "no usable node inside the pinned zone"
	default:
		return "hard constraints eliminate every candidate"
	}
}

// classifyJointFailure builds a CONSTRAINT_CONFLICT failure from dead-end
// snapshots collected during search — this is where MUTUAL conflicts, visible
// only after simultaneous reservations, surface.
func (d *dfsearch) buildFailure() *model.PlanFailure {
	var failed []model.InstanceFailure
	intentIDs := make([]string, 0, len(d.deadEnds))
	for id := range d.deadEnds {
		intentIDs = append(intentIDs, id)
	}
	sort.Strings(intentIDs)
	for _, id := range intentIDs {
		snapshots := d.deadEnds[id]
		var in model.Intent
		for _, x := range d.order {
			if x.ID == id {
				in = x
			}
		}
		// Classify each dead-end snapshot independently, then majority-vote;
		// ties resolved toward the later pipeline stage (more specific cause).
		tally := map[model.ReasonKind]int{}
		var observed []model.HardReject
		for _, snapRejects := range snapshots {
			byNode := map[string][]model.HardReject{}
			for _, r := range snapRejects {
				byNode[r.NodeID] = append(byNode[r.NodeID], r)
				observed = append(observed, r)
			}
			tally[pipelineCategory(in, d.snap, byNode)]++
		}
		cat := voteCategory(tally)
		failed = append(failed, model.InstanceFailure{
			InstanceID: id,
			Reasons:    withCategory(cat, dedupeRejects(observed)),
		})
	}
	if len(failed) == 0 {
		// Defensive: no feasible assignment and no diagnostics. Never report
		// success for an unknown state.
		return &model.PlanFailure{
			Code:    model.ReasonConstraintConflict,
			Message: "no feasible assignment found and no dead-end diagnostics were collected",
		}
	}
	return &model.PlanFailure{
		Code:      model.ReasonConstraintConflict,
		Message:   fmt.Sprintf("%d instance(s) cannot be placed simultaneously: constraint conflict", len(failed)),
		Instances: failed,
	}
}

func voteCategory(tally map[model.ReasonKind]int) model.ReasonKind {
	var cat model.ReasonKind
	bestN := 0
	for c, n := range tally {
		if n > bestN || (n == bestN && reasonRank(c) > reasonRank(cat)) {
			cat, bestN = c, n
		}
	}
	if cat == "" {
		return model.ReasonInternal
	}
	return cat
}

func dedupeRejects(rs []model.HardReject) []model.HardReject {
	type key struct {
		node string
		code model.ReasonKind
		det  string
	}
	seen := map[key]bool{}
	out := make([]model.HardReject, 0, len(rs))
	for _, r := range rs {
		k := key{r.NodeID, r.Code, r.Detail}
		if seen[k] {
			continue
		}
		seen[k] = true
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].NodeID != out[j].NodeID {
			return out[i].NodeID < out[j].NodeID
		}
		return reasonRank(out[i].Code) < reasonRank(out[j].Code)
	})
	return out
}
