package scheduler

import (
	"errors"
	"fmt"
	"sort"

	"placer/internal/model"
)

// Defaults applied when PlanOptions leaves them zero.
const (
	DefaultMaxPendingForExact = 12
	DefaultSearchBudget       = 200_000
)

// SearchExhausted means the exact search hit its node-visit budget without
// proving optimality or infeasibility. This is an error, not a "no
// solution": callers must not mistake it for a constraint conflict.
type SearchExhausted struct {
	Visits int64
	Budget int64
}

func (e *SearchExhausted) Error() string {
	return fmt.Sprintf("scheduler: search budget exhausted (visits=%d budget=%d)", e.Visits, e.Budget)
}

// solver carries per-run mutable search state and the trace.
type solver struct {
	v          *view
	pending    []model.Instance
	maxExact   int
	budget     int64
	visits     int64
	trace      []model.TraceStep
	bestAssign map[string]string // instance id -> node id
	bestObj    *model.Objective
	budgetOut  bool
	feasible   bool // any complete assignment found (heuristic path)
}

// Plan is the batch placement entry point. Hard constraints are always
// evaluated first; soft scores only rank surviving candidates. When no
// legal complete assignment exists the result carries typed conflicts and
// no decisions.
func Plan(req model.PlanRequest) (*model.PlanResult, error) {
	if err := Validate(req); err != nil {
		return nil, err
	}
	applyOptionDefaults(&req.Options)

	v := newView(req.Nodes, req.Bound, req.Policy, req.Options)
	markEligibility(v, req.Instances)

	s := &solver{
		v:        v,
		pending:  req.Instances,
		maxExact: req.Options.MaxNodesForExact,
		budget:   int64(req.Options.SearchBudget),
	}

	// Record the initial skew snapshot (requirement 2: explicit counting).
	snap := v.domainSnapshot(req.Instances)
	s.trace = append(s.trace, model.TraceStep{Kind: "initial_skew", Skew: snap})

	n := len(req.Instances)
	useExact := n <= s.maxExact
	if useExact {
		// Exact enumeration over small instance sets: branch-and-bound over
		// legal assignments, MRV instance order, score-ordered nodes.
		s.searchExact(0, map[string]string{})
		if s.budgetOut && s.bestAssign == nil {
			return nil, &SearchExhausted{Visits: s.visits, Budget: s.budget}
		}
	} else {
		// Greedy with temporary reservations (requirement 3).
		if !s.greedy() {
			conflicts := s.classifyConflicts()
			s.trace = append(s.trace, model.TraceStep{Kind: "final", Detail: "infeasible"})
			return &model.PlanResult{
				RunID: req.RunID, Feasible: false, Conflicts: conflicts,
				Trace: s.trace, Solver: "greedy",
			}, nil
		}
	}

	if s.bestAssign == nil {
		// Exact path exhausted the whole search tree without a solution.
		conflicts := s.classifyConflicts()
		s.trace = append(s.trace, model.TraceStep{Kind: "final", Detail: "infeasible"})
		return &model.PlanResult{
			RunID: req.RunID, Feasible: false, Conflicts: conflicts,
			Trace: s.trace, Solver: "exact",
		}, nil
	}

	decisions := decisionsFrom(s.bestAssign, req.Instances)
	obj := *s.bestObj
	s.trace = append(s.trace, model.TraceStep{
		Kind: "final", Detail: "feasible", Objective: &obj,
	})
	return &model.PlanResult{
		RunID: req.RunID, Feasible: true, Decisions: decisions,
		Trace: s.trace, Objective: &obj,
		Solver: map[bool]string{true: "exact", false: "greedy"}[useExact],
	}, nil
}

// searchExact performs MRV backtracking. assigned maps instance id -> node
// id for instances already temporarily reserved.
func (s *solver) searchExact(depth int, assigned map[string]string) {
	if s.budgetOut {
		return
	}
	if depth == len(s.pending) {
		obj := globalObjective(s.v, s.pending)
		better := s.bestObj == nil || objectiveLess(obj, *s.bestObj) ||
			(obj == *s.bestObj && tieLexLess(assigned, s.bestAssign, s.pending))
		objCopy := obj
		step := model.TraceStep{
			Kind: "assignment_considered", Objective: &objCopy, ImprovesBest: better,
		}
		s.trace = append(s.trace, step)
		if better {
			s.bestObj = &obj
			s.bestAssign = cloneAssign(assigned)
		}
		return
	}

	idx := s.pickMRV(assigned)
	inst := s.pending[idx]
	candidates := s.orderedCandidates(inst)
	if len(candidates) == 0 {
		s.recordFilters(inst)
		return
	}
	for _, st := range candidates {
		s.visits++
		if s.visits > s.budget {
			s.budgetOut = true
			return
		}
		// Note: no objective-based pruning here. A prefix's skew is not an
		// admissible lower bound (placing more instances can raise the
		// minimum domain load and reduce skew), so pruning on it would be
		// unsound. The budget is the only search cap.
		s.v.reserve(st, inst)
		assigned[inst.ID] = st.n.ID
		s.searchExact(depth+1, assigned)
		delete(assigned, inst.ID)
		s.v.release(st, inst)
		if s.budgetOut {
			return
		}
	}
}

// orderedCandidates returns the hard-legal nodes for inst sorted by the
// greedy score tuple. Rejections are not traced here; callers needing an
// audit call recordFilters.
func (s *solver) orderedCandidates(inst model.Instance) []*nodeState {
	legal, _ := s.v.legalNodes(inst)
	type scored struct {
		st    *nodeState
		score model.NodeScore
	}
	rows := make([]scored, 0, len(legal))
	for _, st := range legal {
		rows = append(rows, scored{st: st, score: s.v.scoreNode(inst, st, s.pending)})
	}
	sort.Slice(rows, func(i, j int) bool {
		return scoreLess(rows[i].score, rows[j].score, rows[i].st.n.ID, rows[j].st.n.ID)
	})
	out := make([]*nodeState, len(rows))
	for i, r := range rows {
		out[i] = r.st
	}
	return out
}

// pickMRV chooses the unassigned instance with the fewest currently legal
// nodes (ties: higher request size, then lexicographic id — deterministic).
func (s *solver) pickMRV(assigned map[string]string) int {
	bestIdx := -1
	var bestCount int
	for i, inst := range s.pending {
		if _, done := assigned[inst.ID]; done {
			continue
		}
		legal, _ := s.v.legalNodes(inst)
		if bestIdx == -1 || len(legal) < bestCount ||
			(len(legal) == bestCount && tieInstanceFirst(inst, s.pending[bestIdx])) {
			bestIdx, bestCount = i, len(legal)
		}
	}
	return bestIdx
}

func tieInstanceFirst(a, b model.Instance) bool {
	sa := a.Request.MilliCPU + a.Request.Memory + a.Request.Storage
	sb := b.Request.MilliCPU + b.Request.Memory + b.Request.Storage
	if sa != sb {
		return sa > sb
	}
	return a.ID < b.ID
}

// greedy places instances one at a time using temporary reservations so
// every later instance sees the earlier ones.
func (s *solver) greedy() bool {
	assigned := map[string]string{}
	for len(assigned) < len(s.pending) {
		idx := s.pickMRV(assigned)
		inst := s.pending[idx]
		candidates := s.orderedCandidates(inst)
		if len(candidates) == 0 {
			s.recordFilters(inst)
			// Undo this batch: greedy returns no partial placement.
			for id, nodeID := range assigned {
				pi := findPending(s.pending, id)
				s.v.release(s.v.byID[nodeID], pi)
			}
			return false
		}
		scores := map[string]model.NodeScore{}
		for _, st := range candidates {
			scores[st.n.ID] = s.v.scoreNode(inst, st, s.pending)
		}
		// candidates[0] is ranked by scoreLess on the current reservation
		// view, so the winner already accounts for earlier placements.
		winner := candidates[0]
		s.v.reserve(winner, inst)
		assigned[inst.ID] = winner.n.ID
		snap := s.v.domainSnapshot(s.pending)
		s.trace = append(s.trace, model.TraceStep{
			Kind: "greedy_pick", InstanceID: inst.ID, NodeID: winner.n.ID,
			NodeScores: scores, Skew: snap,
			Detail: fmt.Sprintf("hard filter left %d nodes; picked by (skew,sum_squares,soft,node_id)", len(candidates)),
		})
	}
	s.bestAssign = cloneAssign(assigned)
	obj := globalObjective(s.v, s.pending)
	s.bestObj = &obj
	return true
}

func findPending(list []model.Instance, id string) model.Instance {
	for _, in := range list {
		if in.ID == id {
			return in
		}
	}
	return model.Instance{ID: id}
}

// recordFilters appends the full per-node rejection list for an instance.
func (s *solver) recordFilters(inst model.Instance) {
	_, rej := s.v.legalNodes(inst)
	for _, r := range rej {
		s.trace = append(s.trace, model.TraceStep{
			Kind: "filter", InstanceID: r.InstanceID, NodeID: r.NodeID,
			Accepted: boolPtr(false), Code: r.Code, Detail: r.Detail,
		})
	}
}

// classifyConflicts produces per-instance failure categories.
//
//  1. Each instance is tested alone on a pristine world: intrinsically
//     illegal instances get their dominant ready-node rejection.
//  2. Each remaining instance is tested in a batch with all OTHER pending
//     instances but without itself. If that reduced batch is feasible, the
//     instance is a genuine member of the unschedulable set; its category
//     is derived from what blocks it when the other instances are placed
//     (resources vs group conflict).
//  3. Otherwise the unschedulability is independent of this instance; it
//     is reported under the category diagnosed for its peers rather than
//     blamed with a misleading code.
func (s *solver) classifyConflicts() []model.Conflict {
	pv := newViewNodesFrom(s.v, s.pending)
	out := map[string]model.Conflict{}

	aloneLegal := map[string]bool{}
	for _, inst := range s.pending {
		legal, rejections := pv.legalNodes(inst)
		if len(legal) == 0 {
			out[inst.ID] = summarizeIntrinsic(inst, rejections)
		} else {
			aloneLegal[inst.ID] = true
		}
	}

	// Pairwise mutual-exclusion attribution (blocked-by peers).
	mutual := map[string]map[string]bool{}
	var liveIDs []string
	for _, inst := range s.pending {
		if aloneLegal[inst.ID] {
			liveIDs = append(liveIDs, inst.ID)
		}
	}
	sort.Strings(liveIDs)
	for i := 0; i < len(liveIDs); i++ {
		for j := i + 1; j < len(liveIDs); j++ {
			a := findPending(s.pending, liveIDs[i])
			b := findPending(s.pending, liveIDs[j])
			if !pairFeasible(pv, a, b) {
				add(mutual, a.ID, b.ID)
				add(mutual, b.ID, a.ID)
			}
		}
	}

	// For each live-but-batch-infeasible instance, place every OTHER pending
	// instance on a fresh world and inspect what remains legal for it.
	for _, id := range liveIDs {
		if _, bad := out[id]; bad {
			continue
		}
		others := without(s.pending, id)
		w := cloneWorld(pv)
		markEligibility(w, others)
		helper := &solver{v: w, pending: others, maxExact: len(others) + 1, budget: s.budget}
		helper.searchExact(0, map[string]string{})
		if helper.bestAssign == nil {
			// Batch fails even without this instance: attribute only when
			// pairwise peers exist, otherwise mark it as blocked by the
			// combined unschedulable set (honest, not a fake success).
			if blockers := mutual[id]; len(blockers) > 0 {
				out[id] = model.Conflict{
					InstanceID: id, Code: model.RejectAntiAffinity,
					Detail:    fmt.Sprintf("legal alone but cannot coexist with %v", sortedKeys(blockers)),
					BlockedBy: sortedKeys(blockers),
				}
			}
			continue
		}
		// Others fit; now test this instance against their optimal packing.
		self := findPending(s.pending, id)
		for instID, nodeID := range helper.bestAssign {
			pi := findPending(others, instID)
			w.reserve(w.byID[nodeID], pi)
		}
		legal, rejections := w.legalNodes(self)
		if len(legal) > 0 {
			// Others' optimum happens to leave room, yet the joint batch was
			// infeasible — a higher-order interaction.
			continue
		}
		c := summarizeIntrinsic(self, rejections)
		if blockers := mutual[id]; len(blockers) > 0 && c.Code != model.RejectResources {
			c.Code = model.RejectAntiAffinity
			c.BlockedBy = sortedKeys(blockers)
			c.Detail = fmt.Sprintf("legal alone but cannot coexist with %v: %s",
				sortedKeys(blockers), c.Detail)
		}
		out[id] = c
	}

	// Residual instances (batch infeasible without them AND no pairwise
	// blocker): the unschedulable set is larger than a pair; attribute by
	// pairwise peers when present, else keep an explicit combined-constraint
	// category rather than inventing a resource code.
	for _, inst := range s.pending {
		if _, ok := out[inst.ID]; ok {
			continue
		}
		detail := "legal alone and in pairs; excluded by combined batch constraints"
		code := model.RejectAntiAffinity
		if blockers := mutual[inst.ID]; len(blockers) > 0 {
			detail = fmt.Sprintf("legal alone but cannot coexist with %v under current hard rules/resources",
				sortedKeys(blockers))
			out[inst.ID] = model.Conflict{InstanceID: inst.ID, Code: code, Detail: detail,
				BlockedBy: sortedKeys(blockers)}
			continue
		}
		out[inst.ID] = model.Conflict{InstanceID: inst.ID, Code: code, Detail: detail}
	}

	result := make([]model.Conflict, 0, len(s.pending))
	for _, inst := range s.pending {
		result = append(result, out[inst.ID])
	}
	return result
}

func without(list []model.Instance, id string) []model.Instance {
	out := make([]model.Instance, 0, len(list)-1)
	for _, in := range list {
		if in.ID != id {
			out = append(out, in)
		}
	}
	return out
}

// summarizeIntrinsic picks the most actionable category for an instance
// that is illegal on every node even with an empty batch. Only ready
// nodes are considered for the resource/selector/taint family of causes:
// a not-ready node is a generic host outage that would otherwise mask the
// instance-specific reason (e.g. "the only ready node is full" must still
// report insufficient_resources).
func summarizeIntrinsic(inst model.Instance, rejections []model.Rejection) model.Conflict {
	counts := map[model.RejectCode]int{}
	examples := map[model.RejectCode]model.Rejection{}
	readyCount := 0
	for _, r := range rejections {
		if r.Code == model.RejectNodeNotReady {
			counts[r.Code]++
			examples[r.Code] = r
			continue
		}
		readyCount++
		counts[r.Code]++
		examples[r.Code] = r
	}
	// If every node is down, the instance-specific checks never applied.
	if readyCount == 0 {
		ex := examples[model.RejectNodeNotReady]
		return model.Conflict{InstanceID: inst.ID, Code: model.RejectNodeNotReady, Detail: ex.Detail}
	}
	// Among ready nodes pick the dominant instance-specific reason.
	priority := []model.RejectCode{
		model.RejectZoneMismatch,
		model.RejectDomainMissing,
		model.RejectNodeSelector,
		model.RejectTaintNotTolerated,
		model.RejectResources,
		model.RejectAntiAffinity,
		model.RejectAffinity,
	}
	var chosen model.RejectCode
	bestCount := 0
	for _, code := range priority {
		if c := counts[code]; c > bestCount {
			chosen, bestCount = code, c
		}
	}
	ex := examples[chosen]
	return model.Conflict{InstanceID: inst.ID, Code: chosen, Detail: ex.Detail}
}

// pairFeasible reports whether a and b can both be reserved on the same
// pristine world simultaneously.
func pairFeasible(template *view, a, b model.Instance) bool {
	w := cloneWorld(template)
	legalA, _ := w.legalNodes(a)
	for _, stA := range legalA {
		w.reserve(stA, a)
		legalB, _ := w.legalNodes(b)
		if len(legalB) > 0 {
			w.release(stA, a)
			return true
		}
		w.release(stA, a)
	}
	return false
}

func add(m map[string]map[string]bool, a, b string) {
	if m[a] == nil {
		m[a] = map[string]bool{}
	}
	m[a][b] = true
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// globalObjective evaluates the objective tuple of the current view.
// Instances remaining unassigned only affect the result through the call
// site, which evaluates at complete assignments during exact search.
func globalObjective(v *view, pending []model.Instance) model.Objective {
	snap := v.domainSnapshot(pending)
	loads := map[string]int{}
	counted := v.countedDomainSet(pending)
	sumSquares := 0
	for _, st := range v.nodes {
		d, ok := st.n.DomainValue(v.key)
		if !ok || !counted[d] {
			continue
		}
		loads[d] += len(st.occup)
	}
	for _, l := range loads {
		sumSquares += l * l
	}
	soft := 0
	for _, st := range v.nodes {
		domain, ok := st.n.DomainValue(v.key)
		if !ok {
			continue
		}
		for _, occ := range st.occup {
			for i, r := range v.gi.rules {
				if r.Mode != model.ModeSoft {
					continue
				}
				val, has := occ.groups[r.Group]
				if !has {
					continue
				}
				for _, other := range v.gi.members[i][val] {
					if other >= occ.id {
						continue // count each unordered pair once
					}
					od := v.gi.domainsOf[i][other]
					if !r.Affinity && od == domain {
						soft++
					}
					if r.Affinity && od != "" && od != domain {
						soft++
					}
				}
			}
		}
	}
	return model.Objective{Skew: snap.Skew, SumSquares: sumSquares, SoftGroups: soft}
}

func objectiveLess(a, b model.Objective) bool {
	if a.Skew != b.Skew {
		return a.Skew < b.Skew
	}
	if a.SumSquares != b.SumSquares {
		return a.SumSquares < b.SumSquares
	}
	return a.SoftGroups < b.SoftGroups
}

// tieLexLess compares two complete assignments lexicographically by
// (instance id -> node id) to make the optimum unique regardless of search
// order.
func tieLexLess(a, b map[string]string, pending []model.Instance) bool {
	if b == nil {
		return true
	}
	ids := make([]string, 0, len(pending))
	for _, in := range pending {
		ids = append(ids, in.ID)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if a[id] != b[id] {
			return a[id] < b[id]
		}
	}
	return false
}

func cloneAssign(m map[string]string) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func decisionsFrom(assign map[string]string, pending []model.Instance) []model.Decision {
	ids := make([]string, 0, len(pending))
	for _, in := range pending {
		ids = append(ids, in.ID)
	}
	sort.Strings(ids)
	out := make([]model.Decision, 0, len(ids))
	for _, id := range ids {
		out = append(out, model.Decision{InstanceID: id, NodeID: assign[id]})
	}
	return out
}

func boolPtr(b bool) *bool { return &b }

// applyOptionDefaults fills zero-valued options.
func applyOptionDefaults(o *model.PlanOptions) {
	if o.SpreadTopologyKey == "" {
		o.SpreadTopologyKey = "zone"
	}
	if o.SkewDomainMode == "" {
		o.SkewDomainMode = modeConfigured
	}
	if o.MaxNodesForExact == 0 {
		o.MaxNodesForExact = DefaultMaxPendingForExact
	}
	if o.SearchBudget == 0 {
		o.SearchBudget = DefaultSearchBudget
	}
}

var errEmptyInstances = errors.New("plan: instances list is empty")
