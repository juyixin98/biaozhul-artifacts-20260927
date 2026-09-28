package scheduler

import (
	"fmt"
	"sort"
	"strings"

	"placer/internal/model"
)

// Validate checks the structural validity of a plan request. Validation
// failures are caller errors (bad input), distinct from scheduling
// conflicts (valid input, no feasible placement).
func Validate(req model.PlanRequest) error {
	var problems []string
	if len(req.Instances) == 0 {
		problems = append(problems, "instances list is empty")
	}
	if len(req.Nodes) == 0 {
		problems = append(problems, "nodes list is empty")
	}
	if err := req.Policy.Validate(); err != nil {
		problems = append(problems, err.Error())
	}
	nodeIDs := map[string]int{}
	for i, n := range req.Nodes {
		if err := n.Validate(); err != nil {
			problems = append(problems, err.Error())
		}
		nodeIDs[n.ID] = i
	}
	instIDs := map[string]bool{}
	for _, in := range req.Instances {
		if err := in.Validate(); err != nil {
			problems = append(problems, err.Error())
		}
		if in.State != "" && in.State != model.StatePending {
			problems = append(problems, fmt.Sprintf("instance %q must be pending for Plan, got %s", in.ID, in.State))
		}
		if in.NodeID != "" {
			problems = append(problems, fmt.Sprintf("instance %q already has node_id %q", in.ID, in.NodeID))
		}
		if instIDs[in.ID] {
			problems = append(problems, fmt.Sprintf("duplicate instance id %q", in.ID))
		}
		instIDs[in.ID] = true
	}
	boundIDs := map[string]bool{}
	for _, b := range req.Bound {
		if b.InstanceID == "" {
			problems = append(problems, "binding with empty instance id")
			continue
		}
		if boundIDs[b.InstanceID] {
			problems = append(problems, fmt.Sprintf("duplicate binding for instance %q", b.InstanceID))
		}
		boundIDs[b.InstanceID] = true
		if instIDs[b.InstanceID] {
			problems = append(problems, fmt.Sprintf("instance %q is both pending and bound", b.InstanceID))
		}
		if _, ok := nodeIDs[b.NodeID]; !ok {
			problems = append(problems, fmt.Sprintf("binding %q references unknown node %q", b.InstanceID, b.NodeID))
		}
		if err := b.Request.Validate(); err != nil {
			problems = append(problems, "binding "+b.InstanceID+": "+err.Error())
		}
	}
	switch req.Options.SkewDomainMode {
	case "", modeConfigured, modeEligible:
	default:
		problems = append(problems, "unknown skew_domain_mode "+req.Options.SkewDomainMode)
	}
	// Spread key must exist on at least one node; otherwise the scheduler
	// cannot define a single domain universe (missing-domain is explicit).
	if req.Options.SpreadTopologyKey != "" {
		seen := false
		for _, n := range req.Nodes {
			if _, ok := n.DomainValue(req.Options.SpreadTopologyKey); ok {
				seen = true
				break
			}
		}
		if !seen {
			problems = append(problems, "no node carries spread topology key "+req.Options.SpreadTopologyKey)
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("plan validation failed: %s", strings.Join(problems, "; "))
	}
	return nil
}

// markEligibility computes the batch-wide pre-filter used by "eligible"
// skew mode: a node is eligible when at least one pending instance passes
// status/zone/selector/taint checks on it (resources and group rules are
// deliberately not included — they depend on other placements).
func markEligibility(v *view, pending []model.Instance) {
	for _, st := range v.nodes {
		st.eligible = false
	}
	for _, inst := range pending {
		for _, st := range v.nodes {
			n := st.n
			if n.Status != model.NodeReady {
				continue
			}
			if inst.Zone != "" && n.Zone != inst.Zone {
				continue
			}
			if !selectorMatches(inst, n) {
				continue
			}
			if hardOK, _ := n.ToleratesAll(inst.Tolerations); !hardOK {
				continue
			}
			st.eligible = true
		}
	}
}

func selectorMatches(inst model.Instance, n model.Node) bool {
	for k, want := range inst.NodeSelector {
		if got, ok := n.Labels[k]; !ok || got != want {
			return false
		}
	}
	return true
}

// sortedNodeStates returns nodes ordered by ID for deterministic cloning.
func sortedNodeStates(v *view) []*nodeState {
	out := make([]*nodeState, len(v.nodes))
	copy(out, v.nodes)
	sort.Slice(out, func(i, j int) bool { return out[i].n.ID < out[j].n.ID })
	return out
}
