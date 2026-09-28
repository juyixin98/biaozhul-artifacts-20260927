// Package scheduler implements the node placement decision core.
//
// Design contract (see acceptance rules):
//
//  1. HARD constraints are applied as a filter BEFORE any scoring. A node that
//     fails a hard constraint can never be selected regardless of soft score.
//  2. The set of domains participating in skew computation (D*) is computed
//     explicitly: zones with >=1 eligible node, plus configured truly-empty
//     declared zones. Zones whose nodes are ALL ineligible are NOT skew
//     domains (and pinned requests targeting them fail hard).
//  3. Simultaneous placement uses a mutable temporary-reservation table so
//     instances placed earlier in one request can violate nothing for
//     instances placed later.
//  4. When no legal assignment exists, Solve returns a structured
//     CONSTRAINT_CONFLICT failure naming instances and reasons — never an
//     arbitrary placement.
//
// For small requests the solver enumerates EVERY feasible complete assignment
// exhaustively (depth-first, deterministic order) and keeps the lexicographic
// minimum of the soft-score vector. Above configurable size caps it falls
// back to a deterministic most-constrained-first greedy search; the fallback
// is recorded in the decision trace.
package scheduler

import (
	"fmt"
	"sort"

	"opp284/placement/internal/model"
)

// RunningInstance is an instance already placed in the cluster. Running
// instances hold resources, carry labels for affinity interpretation and are
// members of affinity groups.
type RunningInstance struct {
	ID              string
	NodeID          string
	Request         model.Resources
	AffinityGroups  []string
}

// Snapshot is the immutable-for-one-solve cluster state.
type Snapshot struct {
	Nodes     []model.Node
	Running   []RunningInstance
	Groups    []model.Group
	// DeclaredZones optionally lists zones the operator knows about even when
	// they currently contain no node (empty-domain skew semantics).
	DeclaredZones []string
}

// Replacement pairs a new intent with the running instance it replaces.
// The running instance's resources are HELD during planning (conservative
// rolling update): the new copy must reserve capacity on a different node
// while the old copy still runs.
type Replacement struct {
	OldID string
}

// Request is one placement computation request.
type Request struct {
	PlanID       string
	Intents      []model.Intent
	Replacements map[string]Replacement // keyed by NEW intent id
	// AllowRecreate lets a replacement evict the old instance first and reuse
	// its node/capacity. With false, replacements that cannot fit while the
	// old copy runs fail with ROLLING_REPLACEMENT_BLOCKED.
	AllowRecreate bool
}

// CandidateStep records hard-filter evaluation for one (instance, node) pair.
type CandidateStep struct {
	NodeID       string            `json:"node_id"`
	Zone         string            `json:"zone"`
	Accepted     bool              `json:"accepted"`
	HardRejects  []model.HardReject `json:"hard_rejects,omitempty"`
	// FreeAfter is the free capacity vector seen for this evaluation AFTER
	// temporary reservations of earlier simultaneous placements.
	FreeAfter    model.Resources   `json:"free_after,omitempty"`
}

// Step is one traceable decision step.
type Step struct {
	Stage       string          `json:"stage"`
	Detail      string          `json:"detail,omitempty"`
	InstanceID  string          `json:"instance_id,omitempty"`
	Candidates  []CandidateStep `json:"candidates,omitempty"`
	ChosenNode  string          `json:"chosen_node,omitempty"`
	ScoreBefore *ScoreVector    `json:"score_before,omitempty"`
	ScoreAfter  *ScoreVector    `json:"score_after,omitempty"`
}

// Decision is the result of a successful solve.
type Decision struct {
	PlanID       string
	Placements   []model.Placement
	Score        ScoreVector
	Exhaustive   bool
	LeafVisits   int
	Steps        []Step
	// ParticipatingDomains is D* used for skew scoring.
	ParticipatingDomains []string
}

// Solve computes a legal placement or returns a *model.PlanFailure.
//
// On hard failure the returned Decision is nil. The trace returned alongside
// a failure still contains the filter steps leading to it, which is what the
// failure tests assert on ("verify the reason for every choice").
func Solve(snap Snapshot, req Request, limits SearchLimits) (*Decision, []Step, *model.PlanFailure) {
	steps := []Step{}
	if err := validateInput(snap, req); err != nil {
		steps = append(steps, Step{Stage: "validate", Detail: err.Error()})
		return nil, steps, err
	}

	idx := buildIndex(snap)

	// Static, per-instance hard filtering against running + zero reservations.
	static := filterAllStatic(snap, req, idx, &steps)

	// Fast failure: an instance with zero candidates BEFORE joint placement
	// can never succeed. Classify from the collected hard rejects.
	if f := classifyStaticFailure(req, snap, static); f != nil {
		steps = append(steps, Step{Stage: "fail", Detail: "static hard-constraint filter eliminated every node for at least one instance"})
		return nil, steps, f
	}

	order := orderIntents(req.Intents, static)
	d := &dfsearch{
		snap:   snap,
		req:    req,
		idx:    idx,
		limits: limits,
		static: static,
		order:  order,
		steps:  &steps,
	}
	decision, fail := d.run()
	if fail != nil {
		return nil, steps, fail
	}
	return decision, steps, nil
}

func validateInput(snap Snapshot, req Request) *model.PlanFailure {
	if len(snap.Nodes) == 0 {
		return &model.PlanFailure{
			Code:    model.ReasonNoNodesInCluster,
			Message: "cluster snapshot contains zero nodes",
		}
	}
	if len(req.Intents) == 0 {
		return &model.PlanFailure{
			Code:    model.ReasonEmptyRequest,
			Message: "request contains zero intents",
		}
	}
	nodeIDs := map[string]bool{}
	for _, n := range snap.Nodes {
		if err := n.Validate(); err != nil {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "invalid node: " + err.Error()}
		}
		if nodeIDs[n.ID] {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "duplicate node id " + n.ID}
		}
		nodeIDs[n.ID] = true
	}
	intentIDs := map[string]bool{}
	for _, in := range req.Intents {
		if err := in.Validate(); err != nil {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "invalid intent: " + err.Error()}
		}
		if intentIDs[in.ID] {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "duplicate intent id " + in.ID}
		}
		intentIDs[in.ID] = true
		if _, ok := req.Replacements[in.ID]; ok {
			r := req.Replacements[in.ID]
			if !intentIDsReplaceRunning(snap, r.OldID) {
				return &model.PlanFailure{Code: model.ReasonInternal, Message: fmt.Sprintf("intent %s replaces unknown running instance %s", in.ID, r.OldID)}
			}
		}
	}
	// Group validation.
	gm := map[string]model.Group{}
	for _, g := range snap.Groups {
		if g.ID == "" {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "group with empty id"}
		}
		if _, dup := gm[g.ID]; dup {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "duplicate group id " + g.ID}
		}
		if g.Mode != model.GroupAffinity && g.Mode != model.GroupAntiAffinity {
			return &model.PlanFailure{Code: model.ReasonInternal, Message: "group " + g.ID + " has invalid mode " + string(g.Mode)}
		}
		gm[g.ID] = g
	}
	for _, in := range req.Intents {
		for _, gid := range in.AffinityGroups {
			if _, ok := gm[gid]; !ok {
				return &model.PlanFailure{Code: model.ReasonInternal, Message: fmt.Sprintf("intent %s references unknown group %s", in.ID, gid)}
			}
			if !contains(gm[gid].MemberIDs, in.ID) {
				return &model.PlanFailure{Code: model.ReasonInternal, Message: fmt.Sprintf("intent %s claims group %s but is not listed as a member", in.ID, gid)}
			}
		}
	}
	return nil
}

func intentIDsReplaceRunning(snap Snapshot, oldID string) bool {
	for _, r := range snap.Running {
		if r.ID == oldID {
			return true
		}
	}
	return false
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// staticCandidates holds per-intent candidate nodes after static filtering.
type staticCandidates struct {
	intent model.Intent
	nodes  []string // accepted node ids, sorted
	// rejected records every rejected node with the concrete hard rejects.
	rejected map[string][]model.HardReject
}

func filterAllStatic(snap Snapshot, req Request, idx *index, steps *[]Step) map[string]staticCandidates {
	out := map[string]staticCandidates{}
	// Baseline reservations: everything running, except old copies that a
	// recreate-style replacement is allowed to evict first.
	base := newReservations()
	if req.AllowRecreate {
		// Recompute held usage from running minus evicted old copies.
		evict := map[string]bool{}
		for _, in := range req.Intents {
			if rp, ok := req.Replacements[in.ID]; ok {
				evict[rp.OldID] = true
			}
		}
		idx.held = map[string]model.Resources{}
		for _, n := range idx.nodesSorted {
			idx.held[n.ID] = n.Used.Clone()
		}
		for _, r := range snap.Running {
			if evict[r.ID] {
				continue
			}
			idx.held[r.NodeID] = idx.held[r.NodeID].Add(r.Request)
		}
	}

	for _, in := range req.Intents {
		// A rolling replacement (recreate disallowed) cannot reuse its old
		// node: the old copy still runs there. Encode this as an extra static
		// reject rule via a "blocked nodes" overlay.
		var blockedOldNode string
		if rp, ok := req.Replacements[in.ID]; ok && !req.AllowRecreate {
			if old, ok := idx.runningByID[rp.OldID]; ok {
				blockedOldNode = old.NodeID
			}
		}

		accepted, candSteps := staticFilter(in, idx, base, blockedOldNode)
		ids := make([]string, 0, len(accepted))
		rejected := map[string][]model.HardReject{}
		for _, cs := range candSteps {
			if cs.Accepted {
				ids = append(ids, cs.NodeID)
			} else {
				rejected[cs.NodeID] = cs.HardRejects
			}
		}
		sort.Strings(ids)
		out[in.ID] = staticCandidates{intent: in, nodes: ids, rejected: rejected}
		*steps = append(*steps, Step{
			Stage:      "hard_filter",
			InstanceID: in.ID,
			Detail: fmt.Sprintf("static hard filter: %d/%d nodes legal before simultaneous reservations",
				len(ids), len(snap.Nodes)),
			Candidates: candSteps,
		})
	}
	return out
}

// staticFilter runs hard constraints at depth zero, plus rolling-replacement
// node blocking (new copy must not land on the old node while the old copy is
// still running).
func staticFilter(in model.Intent, idx *index, base *reservations, blockedOldNode string) ([]model.Node, []CandidateStep) {
	accepted, steps := hardFilter(in, idx, base)
	return applyBlockedNode(accepted, blockedOldNode), applyBlockedStep(steps, blockedOldNode)
}

// applyBlockedNode removes the old copy's node from candidate nodes (rolling
// replacement, recreate disallowed).
func applyBlockedNode(accepted []model.Node, blockedOldNode string) []model.Node {
	if blockedOldNode == "" {
		return accepted
	}
	filtered := make([]model.Node, 0, len(accepted))
	for _, n := range accepted {
		if n.ID != blockedOldNode {
			filtered = append(filtered, n)
		}
	}
	return filtered
}

// applyBlockedStep annotates candidate trace steps with the rolling block.
func applyBlockedStep(steps []CandidateStep, blockedOldNode string) []CandidateStep {
	if blockedOldNode == "" {
		return steps
	}
	for i := range steps {
		if steps[i].NodeID == blockedOldNode {
			steps[i].Accepted = false
			steps[i].HardRejects = append(steps[i].HardRejects, model.HardReject{
				Code:   model.ReasonRollingBlocked,
				NodeID: blockedOldNode,
				Detail: "replacement must land elsewhere while the old copy still runs (allow_recreate=false)",
			})
		}
	}
	return steps
}
