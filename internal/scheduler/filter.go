package scheduler

import (
	"fmt"
	"sort"
	"strings"

	"placer/internal/model"
)

// occupant is an instance occupying (or temporarily reserving) a node.
type occupant struct {
	id      string
	groups  map[string]string
	request model.Resources
	// pending distinguishes temporary reservations within one batch from
	// previously bound instances.
	pending bool
}

// groupIndex answers group-rule questions over all occupants in one view.
type groupIndex struct {
	rules []model.GroupRule
	// members[ruleIdx][groupValue] = occupant IDs carrying that value.
	members []map[string][]string
	// domainsOf[ruleIdx] maps occupantID -> domain value they occupy.
	domainsOf []map[string]string
}

func buildGroupIndex(rules []model.GroupRule) groupIndex {
	gi := groupIndex{
		rules:     rules,
		members:   make([]map[string][]string, len(rules)),
		domainsOf: make([]map[string]string, len(rules)),
	}
	for i := range rules {
		gi.members[i] = map[string][]string{}
		gi.domainsOf[i] = map[string]string{}
	}
	return gi
}

// nodeState is the mutable per-node state of one placement view.
type nodeState struct {
	n     model.Node
	used  model.Resources
	occup []occupant
	// eligible is precomputed per batch: the node passes status/zone/
	// selector/taint for at least one pending instance (resource and group
	// filters are intentionally excluded). Used only for skew domain
	// universe accounting in "eligible" mode.
	eligible bool
}

// view is one placement world: bound occupants plus temporary reservations.
type view struct {
	nodes   []*nodeState
	byID    map[string]*nodeState
	gi      groupIndex
	key     string
	options model.PlanOptions
}

func newView(nodes []model.Node, bound []model.Binding, policy model.Policy, options model.PlanOptions) *view {
	v := &view{
		byID:    map[string]*nodeState{},
		gi:      buildGroupIndex(policy.Groups),
		key:     options.SpreadTopologyKey,
		options: options,
	}
	for i := range nodes {
		st := &nodeState{n: nodes[i]}
		v.nodes = append(v.nodes, st)
		v.byID[nodes[i].ID] = st
	}
	for _, b := range bound {
		st := v.byID[b.NodeID]
		if st == nil {
			// Stored bindings referencing a missing node are kept visible:
			// validation rejects this explicitly in Plan instead of
			// silently dropping the occupancy.
			continue
		}
		occ := occupant{id: b.InstanceID, groups: b.Groups, request: b.Request}
		st.occup = append(st.occup, occ)
		st.used = st.used.Add(b.Request)
		v.indexOccupant(st, occ)
	}
	return v
}

func (v *view) indexOccupant(st *nodeState, occ occupant) {
	domain, ok := st.n.DomainValue(v.key)
	for i, r := range v.gi.rules {
		val, has := occ.groups[r.Group]
		if !has {
			continue
		}
		v.gi.members[i][val] = append(v.gi.members[i][val], occ.id)
		if ok {
			v.gi.domainsOf[i][occ.id] = domain
		}
	}
}

func (v *view) deindexOccupant(st *nodeState, occ occupant) {
	domain, ok := st.n.DomainValue(v.key)
	_ = domain
	for i, r := range v.gi.rules {
		val, has := occ.groups[r.Group]
		if !has {
			continue
		}
		ids := v.gi.members[i][val]
		for j, id := range ids {
			if id == occ.id {
				v.gi.members[i][val] = append(ids[:j], ids[j+1:]...)
				break
			}
		}
		if ok {
			delete(v.gi.domainsOf[i], occ.id)
		}
	}
}

// reserve temporarily places inst on st (used by the simultaneous-placement
// search so that later instances see the occupancy and cannot violate each
// other).
func (v *view) reserve(st *nodeState, inst model.Instance) {
	occ := occupant{id: inst.ID, groups: inst.Groups, request: inst.Request, pending: true}
	st.occup = append(st.occup, occ)
	st.used = st.used.Add(inst.Request)
	v.indexOccupant(st, occ)
}

// release undoes a reserve.
func (v *view) release(st *nodeState, inst model.Instance) {
	for i, o := range st.occup {
		if o.id == inst.ID {
			st.occup = append(st.occup[:i], st.occup[i+1:]...)
			st.used = st.used.Sub(inst.Request)
			v.deindexOccupant(st, o)
			return
		}
	}
}

// hardCheck applies ONLY hard constraints. Soft rules never appear here and
// can never turn a rejection into an acceptance. Returns a RejectCode
// (RejectNone == accepted) and a human-readable detail plus the blocking
// occupant IDs.
func (v *view) hardCheck(inst model.Instance, st *nodeState) (model.RejectCode, string, []string) {
	n := st.n
	if n.Status != model.NodeReady {
		return model.RejectNodeNotReady, fmt.Sprintf("node %q status=%s", n.ID, n.Status), nil
	}
	if inst.Zone != "" && n.Zone != inst.Zone {
		return model.RejectZoneMismatch, fmt.Sprintf("instance requires zone %q, node is in %q", inst.Zone, n.Zone), nil
	}
	for k, want := range inst.NodeSelector {
		got, ok := n.Labels[k]
		if !ok || got != want {
			return model.RejectNodeSelector, fmt.Sprintf("selector %s=%s unmatched (got %q, present=%v)", k, want, got, ok), nil
		}
	}
	if hardOK, _ := n.ToleratesAll(inst.Tolerations); !hardOK {
		var keys []string
		for _, t := range n.Taints {
			matched := false
			for _, tol := range inst.Tolerations {
				if tol.Tolerates(t) {
					matched = true
					break
				}
			}
			if !matched && t.Effect == model.TaintNoSchedule {
				keys = append(keys, t.Key+"="+t.Value)
			}
		}
		return model.RejectTaintNotTolerated, "untolerated no_schedule taints: " + strings.Join(keys, ","), nil
	}
	if free := n.Capacity.Sub(st.used); !free.Fits(inst.Request) {
		return model.RejectResources, "missing " + strings.Join(free.Missing(inst.Request), ","), nil
	}

	nodeDomain, domainOK := n.DomainValue(v.key)
	for i, r := range v.gi.rules {
		val, participates := inst.Groups[r.Group]
		if !participates || r.Mode != model.ModeHard {
			continue
		}
		if !domainOK {
			return model.RejectDomainMissing,
				fmt.Sprintf("node %q has no domain label %q required by hard rule group=%s", n.ID, v.key, r.Group), nil
		}
		var blockers []string
		for _, other := range v.gi.members[i][val] {
			if other == inst.ID {
				continue
			}
			otherDomain := v.gi.domainsOf[i][other]
			if !r.Affinity && otherDomain == nodeDomain {
				blockers = append(blockers, other)
			}
			if r.Affinity && otherDomain != "" && otherDomain != nodeDomain {
				blockers = append(blockers, other)
			}
		}
		if len(blockers) > 0 {
			sort.Strings(blockers)
			if r.Affinity {
				return model.RejectAffinity,
					fmt.Sprintf("hard affinity group=%s=%s requires domain %q", r.Group, val, v.memberDomain(i, val, inst.ID)),
					blockers
			}
			return model.RejectAntiAffinity,
				fmt.Sprintf("hard anti-affinity group=%s=%s already present in domain %q", r.Group, val, nodeDomain),
				blockers
		}
	}
	return model.RejectNone, "", nil
}

// memberDomain returns the domain occupied by existing members of a group
// value, used to explain a hard-affinity rejection.
func (v *view) memberDomain(ruleIdx int, val, selfID string) string {
	for _, id := range v.gi.members[ruleIdx][val] {
		if id != selfID {
			if d := v.gi.domainsOf[ruleIdx][id]; d != "" {
				return d
			}
		}
	}
	return ""
}

// legalNodes returns every node surviving hard filters for inst, paired
// with the rejection reason for the nodes that did not.
func (v *view) legalNodes(inst model.Instance) ([]*nodeState, []model.Rejection) {
	var legal []*nodeState
	var rej []model.Rejection
	for _, st := range v.nodes {
		if code, detail, _ := v.hardCheck(inst, st); code == model.RejectNone {
			legal = append(legal, st)
		} else {
			rej = append(rej, model.Rejection{InstanceID: inst.ID, NodeID: st.n.ID, Code: code, Detail: detail})
		}
	}
	return legal, rej
}
