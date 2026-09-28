// Package oracle is an INDEPENDENT reference placement implementation used
// only by tests. It deliberately shares no code with internal/scheduler:
// no view, no group index, no MRV, no scoring table. It enumerates every
// instance->node combination naively, re-checks each complete assignment
// from scratch with its own feasibility logic, and computes the objective
// independently.
//
// If the production scheduler agrees with this second implementation on
// random small clusters, the expected answers are not merely answers the
// core produced about itself.
//
// Supported semantics (intentionally a subset, enough for cross-checks):
//   - spread topology key "zone" (or a node-label key), default domain
//     counting: union of domains over ALL configured nodes, empty domains
//     included;
//   - hard/soft group affinity and anti-affinity on that same key;
//   - resources, zone request, node selector, taints/tolerations, status.
package oracle

import (
	"sort"

	"placer/internal/model"
)

// Input is the reference solver's world.
type Input struct {
	Nodes     []model.Node
	Instances []model.Instance
	Bound     []model.Binding
	Rules     []model.GroupRule
	Key       string
}

// Assignment maps instance id -> node id.
type Assignment map[string]string

type verdict struct {
	ok   bool
	code model.RejectCode
}

// domainOf is the oracle's own domain lookup.
func (in Input) domainOf(n model.Node) (string, bool) {
	key := in.Key
	if key == "" {
		key = "zone"
	}
	return n.DomainValue(key)
}

// nodeUsed recomputes occupancy of node idx from bound + one assignment.
func (in Input) nodeUsed(nodeID string, a Assignment) model.Resources {
	var used model.Resources
	for _, b := range in.Bound {
		if b.NodeID == nodeID {
			used = used.Add(b.Request)
		}
	}
	for instID, nID := range a {
		if nID != nodeID {
			continue
		}
		for _, ins := range in.Instances {
			if ins.ID == instID {
				used = used.Add(ins.Request)
			}
		}
	}
	return used
}

// feasibleOne checks one (instance, node) pair from scratch given the full
// assignment. This is the independent hard-constraint definition.
func (in Input) feasibleOne(ins model.Instance, n model.Node, a Assignment) verdict {
	if n.Status != model.NodeReady {
		return verdict{false, model.RejectNodeNotReady}
	}
	if ins.Zone != "" && n.Zone != ins.Zone {
		return verdict{false, model.RejectZoneMismatch}
	}
	for k, want := range ins.NodeSelector {
		if got, ok := n.Labels[k]; !ok || got != want {
			return verdict{false, model.RejectNodeSelector}
		}
	}
	// Taints: independent toleration loop.
	for _, t := range n.Taints {
		if t.Effect != model.TaintNoSchedule {
			continue
		}
		tol := false
		for _, x := range ins.Tolerations {
			if x.Key == t.Key && (x.Value == "" || x.Value == t.Value) {
				tol = true
				break
			}
		}
		if !tol {
			return verdict{false, model.RejectTaintNotTolerated}
		}
	}
	if free := n.Capacity.Sub(in.nodeUsed(n.ID, a)); !free.Fits(ins.Request) {
		return verdict{false, model.RejectResources}
	}

	nodeDomain, domOK := in.domainOf(n)
	for _, r := range in.Rules {
		if r.Mode != model.ModeHard {
			continue
		}
		val, participates := ins.Groups[r.Group]
		if !participates {
			continue
		}
		if !domOK {
			return verdict{false, model.RejectDomainMissing}
		}
		// Inspect every OTHER placed/bound member of the same group value.
		for _, other := range in.allGroupMembers(r.Group, val, a) {
			if other == ins.ID {
				continue
			}
			otherNode := in.nodeOf(other, a)
			od, ok := in.domainOf(otherNode)
			if !ok {
				continue
			}
			if !r.Affinity && od == nodeDomain {
				return verdict{false, model.RejectAntiAffinity}
			}
			if r.Affinity && od != nodeDomain {
				return verdict{false, model.RejectAffinity}
			}
		}
	}
	return verdict{true, model.RejectNone}
}

// allGroupMembers lists bound and assignment-placed instance ids carrying
// group=value.
func (in Input) allGroupMembers(group, value string, a Assignment) []string {
	var out []string
	for _, b := range in.Bound {
		if b.Groups[group] == value {
			out = append(out, b.InstanceID)
		}
	}
	for _, ins := range in.Instances {
		if _, placed := a[ins.ID]; !placed {
			continue
		}
		if ins.Groups[group] == value {
			out = append(out, ins.ID)
		}
	}
	return out
}

// nodeOf returns the node of a bound or assigned instance id.
func (in Input) nodeOf(instID string, a Assignment) model.Node {
	if nID, ok := a[instID]; ok {
		for _, n := range in.Nodes {
			if n.ID == nID {
				return n
			}
		}
	}
	for _, b := range in.Bound {
		if b.InstanceID == instID {
			for _, n := range in.Nodes {
				if n.ID == b.NodeID {
					return n
				}
			}
		}
	}
	return model.Node{}
}

// Feasible checks a complete (or partial) assignment against the oracle's
// independent rule set.
func (in Input) Feasible(a Assignment) (bool, model.RejectCode) {
	for _, ins := range in.Instances {
		nID, placed := a[ins.ID]
		if !placed {
			continue
		}
		var n model.Node
		found := false
		for _, cand := range in.Nodes {
			if cand.ID == nID {
				n, found = cand, true
				break
			}
		}
		if !found {
			return false, model.RejectNodeNotReady
		}
		if v := in.feasibleOne(ins, n, a); !v.ok {
			return false, v.code
		}
	}
	return true, model.RejectNone
}

// Objective is the oracle's independently computed objective tuple.
func (in Input) Objective(a Assignment) model.Objective {
	key := in.Key
	if key == "" {
		key = "zone"
	}
	// Counted domains: union across ALL configured nodes (default mode).
	counted := map[string]bool{}
	loads := map[string]int{}
	for _, n := range in.Nodes {
		if d, ok := n.DomainValue(key); ok {
			counted[d] = true
			loads[d] = 0
		}
	}
	for _, n := range in.Nodes {
		d, ok := n.DomainValue(key)
		if !ok || !counted[d] {
			continue
		}
		loads[d] += in.occupantCount(n.ID, a)
	}
	max, min, sq := 0, 0, 0
	first := true
	for d := range counted {
		l := loads[d]
		sq += l * l
		if first || l > max {
			max = l
		}
		if first || l < min {
			min = l
		}
		first = false
	}

	soft := 0
	type pair struct{ x, y string }
	countedPair := map[pair]bool{}
	for _, r := range in.Rules {
		if r.Mode != model.ModeSoft {
			continue
		}
		values := map[string][]string{}
		for _, b := range in.Bound {
			if v := b.Groups[r.Group]; v != "" {
				values[v] = append(values[v], b.InstanceID)
			}
		}
		for _, ins := range in.Instances {
			if _, placed := a[ins.ID]; placed {
				if v := ins.Groups[r.Group]; v != "" {
					values[v] = append(values[v], ins.ID)
				}
			}
		}
		for _, ids := range values {
			for i := 0; i < len(ids); i++ {
				for j := i + 1; j < len(ids); j++ {
					p := pair{ids[i], ids[j]}
					if countedPair[p] {
						continue
					}
					countedPair[p] = true
					na := in.nodeOf(ids[i], a)
					nb := in.nodeOf(ids[j], a)
					da, _ := na.DomainValue(key)
					db, _ := nb.DomainValue(key)
					if !r.Affinity && da == db {
						soft++
					}
					if r.Affinity && da != "" && db != "" && da != db {
						soft++
					}
				}
			}
		}
	}
	return model.Objective{Skew: max - min, SumSquares: sq, SoftGroups: soft}
}

func (in Input) occupantCount(nodeID string, a Assignment) int {
	n := 0
	for _, b := range in.Bound {
		if b.NodeID == nodeID {
			n++
		}
	}
	for instID, nID := range a {
		if nID == nodeID {
			_ = instID
			n++
		}
	}
	return n
}

// Result is the oracle answer for one input.
type Result struct {
	Feasible        bool
	Best            Assignment
	Objective       model.Objective
	FeasibleCount   int                         // number of distinct legal complete assignments
	IntrinsicReject map[string]model.RejectCode // instance -> dominant code when alone-illegal
}

// dominantReadyCode mirrors the production classification contract:
// ignore not_ready hosts (generic outage), then pick the most frequent
// instance-specific rejection across READY nodes, with a deterministic
// fixed priority on ties.
func dominantReadyCode(perNode []model.RejectCode) model.RejectCode {
	counts := map[model.RejectCode]int{}
	ready := 0
	for _, c := range perNode {
		if c == model.RejectNodeNotReady {
			counts[c]++
			continue
		}
		ready++
		counts[c]++
	}
	if ready == 0 {
		return model.RejectNodeNotReady
	}
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
	best := 0
	for _, code := range priority {
		if counts[code] > best {
			chosen, best = code, counts[code]
		}
	}
	return chosen
}

// Solve enumerates every full assignment (cartesian product over legal
// node choices per instance) and independently selects the optimum.
func (in Input) Solve() Result {
	res := Result{IntrinsicReject: map[string]model.RejectCode{}}

	// Intrinsic, alone-on-empty-world classification computed independently.
	for _, ins := range in.Instances {
		var perNode []model.RejectCode
		anyLegal := false
		for _, n := range in.Nodes {
			v := in.feasibleOne(ins, n, Assignment{})
			if v.ok {
				anyLegal = true
				break
			}
			perNode = append(perNode, v.code)
		}
		if !anyLegal {
			res.IntrinsicReject[ins.ID] = dominantReadyCode(perNode)
		}
	}

	var best Assignment
	var bestObj model.Objective
	count := 0
	var rec func(idx int, cur Assignment)
	rec = func(idx int, cur Assignment) {
		if idx == len(in.Instances) {
			count++
			obj := in.Objective(cur)
			if best == nil || lessObj(obj, bestObj) ||
				(obj == bestObj && lexLess(cur, best, in.Instances)) {
				best = clone(cur)
				bestObj = obj
			}
			return
		}
		ins := in.Instances[idx]
		for _, n := range in.Nodes {
			// Branch only when this pair is legal given earlier picks.
			if v := in.feasibleOne(ins, n, cur); !v.ok {
				continue
			}
			cur[ins.ID] = n.ID
			rec(idx+1, cur)
			delete(cur, ins.ID)
		}
	}
	rec(0, Assignment{})

	res.FeasibleCount = count
	res.Feasible = count > 0
	res.Best = best
	res.Objective = bestObj
	return res
}

func clone(a Assignment) Assignment {
	out := make(Assignment, len(a))
	for k, v := range a {
		out[k] = v
	}
	return out
}

func lessObj(a, b model.Objective) bool {
	if a.Skew != b.Skew {
		return a.Skew < b.Skew
	}
	if a.SumSquares != b.SumSquares {
		return a.SumSquares < b.SumSquares
	}
	return a.SoftGroups < b.SoftGroups
}

func lexLess(a, b Assignment, list []model.Instance) bool {
	ids := make([]string, 0, len(list))
	for _, in := range list {
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
