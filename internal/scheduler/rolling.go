package scheduler

import (
	"fmt"
	"sort"

	"placer/internal/model"
)

// rollingState is the mutable state of one evict/place sequence search.
//
// Two invariants are maintained after every transition:
//
//	surge       = #new placed while its old counterpart is still bound
//	unavailable = #old evicted before its new counterpart is placed
//
// Transitions:
//
//	place new while old bound      -> surge++
//	place new while old evicted    -> unavailable-- (the slot comes back)
//	evict old while new unplaced   -> unavailable++
//	evict old while new placed     -> surge-- (surplus retired)
type rollingState struct {
	v           *view
	oldInst     map[string]model.Instance // old id -> instance spec
	oldNode     map[string]string         // old id -> current node ("" = evicted)
	newNode     map[string]string         // new id -> node ("" = unplaced)
	replaces    map[string]string         // new id -> old id
	surge       int
	unavailable int
	maxSurge    int
	maxUnavail  int
	pending     []model.Instance // new instances
	trace       []model.TraceStep
	budget      int64
	visits      int64
	budgetOut   bool

	bestOps   []model.ReplaceOp
	bestFinal map[string]string // new id -> node id at the best end state
	bestObj   *model.Objective
}

// Replace plans a rolling replacement. It searches for an interleaved
// evict-old / place-new order that never violates MaxSurge or
// MaxUnavailable and never violates hard constraints at any intermediate
// state. The final placement is optimized with the same objective as Plan;
// among equal final placements, sequences that evict as late as possible
// are preferred (minimize the disruption window).
func Replace(req model.ReplaceRequest) (*model.ReplaceResult, error) {
	if err := validateReplace(req); err != nil {
		return nil, err
	}
	applyOptionDefaults(&req.Options)

	bound := make([]model.Binding, 0, len(req.Old))
	oldInst := map[string]model.Instance{}
	for _, o := range req.Old {
		bound = append(bound, model.Binding{
			InstanceID: o.ID, NodeID: o.NodeID, Request: o.Request, Groups: o.Groups,
		})
		oldInst[o.ID] = o
	}
	v := newView(req.Nodes, bound, req.Policy, req.Options)
	markEligibility(v, req.New)

	rs := &rollingState{
		v:          v,
		oldInst:    oldInst,
		oldNode:    map[string]string{},
		newNode:    map[string]string{},
		replaces:   req.Replaces,
		maxSurge:   req.MaxSurge,
		maxUnavail: req.MaxUnavailable,
		pending:    req.New,
		budget:     int64(req.Options.SearchBudget),
	}
	for _, o := range req.Old {
		rs.oldNode[o.ID] = o.NodeID
	}

	snap := v.domainSnapshot(req.New)
	rs.trace = append(rs.trace, model.TraceStep{Kind: "initial_skew", Skew: snap})

	rs.search(nil)
	if rs.budgetOut && rs.bestOps == nil {
		return nil, &SearchExhausted{Visits: rs.visits, Budget: rs.budget}
	}
	if rs.bestOps == nil {
		conflicts := rs.diagnose()
		rs.trace = append(rs.trace, model.TraceStep{Kind: "final", Detail: "infeasible"})
		return &model.ReplaceResult{
			RunID: req.RunID, Feasible: false, Conflicts: conflicts,
			Trace: rs.trace,
		}, nil
	}

	final := make([]model.Decision, 0, len(req.New))
	for _, n := range req.New {
		final = append(final, model.Decision{InstanceID: n.ID, NodeID: rs.bestFinal[n.ID]})
	}
	sort.Slice(final, func(i, j int) bool { return final[i].InstanceID < final[j].InstanceID })
	obj := globalObjective(v, req.New)
	rs.trace = append(rs.trace, model.TraceStep{Kind: "final", Objective: &obj, Detail: "feasible"})
	return &model.ReplaceResult{
		RunID: req.RunID, Feasible: true, Ops: rs.bestOps,
		Final: final, Objective: &obj, Trace: rs.trace,
	}, nil
}

func (rs *rollingState) done() bool {
	for _, nodeID := range rs.oldNode {
		if nodeID != "" {
			return false
		}
	}
	for _, n := range rs.pending {
		if rs.newNode[n.ID] == "" {
			return false
		}
	}
	return true
}

// search extends the sequence until every old instance is gone and every
// new one placed.
func (rs *rollingState) search(ops []model.ReplaceOp) {
	if rs.budgetOut {
		return
	}
	if rs.done() {
		obj := globalObjective(rs.v, rs.pending)
		better := rs.bestObj == nil || objectiveLess(obj, *rs.bestObj) ||
			(obj == *rs.bestObj && opsLexLess(ops, rs.bestOps))
		rs.trace = append(rs.trace, model.TraceStep{
			Kind: "assignment_considered", Objective: &obj, ImprovesBest: better,
		})
		if better {
			rs.bestObj = &obj
			rs.bestOps = append([]model.ReplaceOp(nil), ops...)
			// Snapshot the final new->node mapping: rs.newNode is mutated
			// during backtracking and would otherwise be empty on return.
			rs.bestFinal = make(map[string]string, len(rs.newNode))
			for k, v := range rs.newNode {
				rs.bestFinal[k] = v
			}
		}
		return
	}

	for _, br := range rs.placeBranches() {
		rs.visits++
		if rs.visits > rs.budget {
			rs.budgetOut = true
			return
		}
		rs.applyPlace(br)
		rs.search(append(ops, model.ReplaceOp{
			Kind: "place_new", InstanceID: br.inst.ID, NodeID: br.st.n.ID,
		}))
		rs.undoPlace(br)
	}

	for _, br := range rs.evictBranches() {
		rs.visits++
		if rs.visits > rs.budget {
			rs.budgetOut = true
			return
		}
		rs.applyEvict(br)
		rs.search(append(ops, model.ReplaceOp{
			Kind: "evict_old", InstanceID: br.oldID,
		}))
		rs.undoEvict(br)
	}
}

type placeBranch struct {
	inst model.Instance
	st   *nodeState
}

type evictBranch struct {
	oldID string
	st    *nodeState
}

// placeBranches lists legal place-new moves in the current state. A move
// is envelope-legal when either the old counterpart is already evicted
// (consumes an unavailable slot) or surge budget remains.
func (rs *rollingState) placeBranches() []placeBranch {
	var out []placeBranch
	for _, inst := range rs.pending {
		if _, placed := rs.newNode[inst.ID]; placed {
			continue
		}
		oldID := rs.replaces[inst.ID]
		if rs.oldNode[oldID] != "" && rs.surge >= rs.maxSurge {
			continue
		}
		legal, _ := rs.v.legalNodes(inst)
		for _, st := range legal {
			out = append(out, placeBranch{inst: inst, st: st})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		si := rs.v.scoreNode(out[i].inst, out[i].st, rs.pending)
		sj := rs.v.scoreNode(out[j].inst, out[j].st, rs.pending)
		if nodeScoreLess(si, sj) || nodeScoreLess(sj, si) {
			return nodeScoreLess(si, sj)
		}
		if out[i].inst.ID != out[j].inst.ID {
			return out[i].inst.ID < out[j].inst.ID
		}
		return out[i].st.n.ID < out[j].st.n.ID
	})
	return out
}

func nodeScoreLess(a, b model.NodeScore) bool {
	if a.Skew != b.Skew {
		return a.Skew < b.Skew
	}
	if a.SumSquares != b.SumSquares {
		return a.SumSquares < b.SumSquares
	}
	return a.SoftGroups < b.SoftGroups
}

// evictBranches lists evict-old moves. Evicting after the replacement is
// placed never consumes unavailable budget (it retires a surge instead).
func (rs *rollingState) evictBranches() []evictBranch {
	var out []evictBranch
	for oldID, nodeID := range rs.oldNode {
		if nodeID == "" {
			continue
		}
		newID := rs.counterpartNew(oldID)
		newPlaced := newID != "" && rs.newNode[newID] != ""
		if !newPlaced && rs.unavailable >= rs.maxUnavail {
			continue
		}
		out = append(out, evictBranch{oldID: oldID, st: rs.v.byID[nodeID]})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].oldID < out[j].oldID })
	return out
}

func (rs *rollingState) counterpartNew(oldID string) string {
	for newID, o := range rs.replaces {
		if o == oldID {
			return newID
		}
	}
	return ""
}

func (rs *rollingState) applyPlace(br placeBranch) {
	oldID := rs.replaces[br.inst.ID]
	if rs.oldNode[oldID] != "" {
		rs.surge++
	} else {
		rs.unavailable--
	}
	rs.v.reserve(br.st, br.inst)
	rs.newNode[br.inst.ID] = br.st.n.ID
}

func (rs *rollingState) undoPlace(br placeBranch) {
	oldID := rs.replaces[br.inst.ID]
	if rs.oldNode[oldID] != "" {
		rs.surge--
	} else {
		rs.unavailable++
	}
	rs.v.release(br.st, br.inst)
	delete(rs.newNode, br.inst.ID)
}

func (rs *rollingState) applyEvict(br evictBranch) {
	newID := rs.counterpartNew(br.oldID)
	newPlaced := newID != "" && rs.newNode[newID] != ""
	if newPlaced {
		rs.surge--
	} else {
		rs.unavailable++
	}
	rs.v.release(br.st, rs.oldInst[br.oldID])
	rs.oldNode[br.oldID] = ""
}

func (rs *rollingState) undoEvict(br evictBranch) {
	newID := rs.counterpartNew(br.oldID)
	newPlaced := newID != "" && rs.newNode[newID] != ""
	if newPlaced {
		rs.surge++
	} else {
		rs.unavailable--
	}
	rs.v.reserve(br.st, rs.oldInst[br.oldID])
	rs.oldNode[br.oldID] = br.st.n.ID
}

// diagnose reports why no sequence exists:
//  1. final-state infeasibility on an empty world, classified exactly like
//     Plan conflicts;
//  2. envelope impossibility (zero surge AND zero unavailable is a deadlock
//     on the very first move);
//  3. otherwise an envelope-conflict for every new instance whose first
//     legal move cannot fit the surge/unavailable limits.
func (rs *rollingState) diagnose() []model.Conflict {
	w := cloneWorld(rs.v)
	markEligibility(w, rs.pending)
	s := &solver{v: w, pending: rs.pending, maxExact: len(rs.pending) + 1, budget: rs.budget}
	s.searchExact(0, map[string]string{})

	var out []model.Conflict
	if s.bestAssign == nil {
		out = append(out, s.classifyConflicts()...)
	}

	if rs.maxSurge == 0 && rs.maxUnavail == 0 {
		for _, n := range rs.pending {
			out = append(out, model.Conflict{
				InstanceID: n.ID,
				Code:       model.RejectAntiAffinity,
				Detail: fmt.Sprintf(
					"rolling envelope deadlock with max_surge=0,max_unavailable=0: new %q cannot bind before old %q leaves and the old cannot leave before the new binds",
					n.ID, rs.replaces[n.ID]),
			})
		}
		return dedupeConflicts(out)
	}

	// No first move possible under the envelope? Attribute per instance.
	canStart := map[string]bool{}
	for _, br := range rs.placeBranches() {
		canStart[br.inst.ID] = true
	}
	// Evicting is itself a valid first move whenever budget allows.
	if rs.maxUnavail > 0 {
		for range rs.evictBranches() {
			// Eviction is always implementable (it only frees resources);
			// mark all instances as able to make progress via eviction.
			for _, n := range rs.pending {
				canStart[n.ID] = true
			}
			break
		}
	}
	for _, n := range rs.pending {
		if canStart[n.ID] {
			continue
		}
		legal, rejections := rs.v.legalNodes(n)
		if len(legal) == 0 {
			c := summarizeIntrinsic(n, rejections)
			c.Detail = "cannot place while old generation remains (surge exhausted): " + c.Detail
			out = append(out, c)
		} else {
			out = append(out, model.Conflict{
				InstanceID: n.ID,
				Code:       model.RejectAntiAffinity,
				Detail: fmt.Sprintf(
					"rolling envelope exhausted: new %q can only coexist with old %q but max_surge=%d is consumed",
					n.ID, rs.replaces[n.ID], rs.maxSurge),
			})
		}
	}

	result := dedupeConflicts(out)
	if len(result) == 0 {
		// Defensive: search failed but diagnosis found nothing. Report this
		// honestly instead of silently "succeeding".
		for _, n := range rs.pending {
			result = append(result, model.Conflict{
				InstanceID: n.ID, Code: model.RejectAntiAffinity,
				Detail: "no feasible rolling sequence found under combined constraints",
			})
		}
	}
	return result
}

func dedupeConflicts(in []model.Conflict) []model.Conflict {
	seen := map[string]bool{}
	var out []model.Conflict
	for _, c := range in {
		key := c.InstanceID + "|" + string(c.Code) + "|" + c.Detail
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].InstanceID != out[j].InstanceID {
			return out[i].InstanceID < out[j].InstanceID
		}
		return out[i].Detail < out[j].Detail
	})
	return out
}

// opsLexLess prefers sequences that evict as late as possible: place_new
// sorts before evict_old, then by instance/node id for determinism.
func opsLexLess(a, b []model.ReplaceOp) bool {
	weight := func(k string) int {
		if k == "place_new" {
			return 0
		}
		return 1
	}
	for i := 0; i < len(a) && i < len(b); i++ {
		wa, wb := weight(a[i].Kind), weight(b[i].Kind)
		if wa != wb {
			return wa < wb
		}
		if a[i].InstanceID != b[i].InstanceID {
			return a[i].InstanceID < b[i].InstanceID
		}
		if a[i].NodeID != b[i].NodeID {
			return a[i].NodeID < b[i].NodeID
		}
	}
	return len(a) < len(b)
}
